"""Read and rewrite the streams inside a media file (the split from app.videos).

This module has the MediaInfo scan and the stored track metadata
(this includes the Dolby Vision profile parse). It has the mkvpropedit
flag editor and the mkvmerge remuxer, with their task wrappers that
hold the locks. It has the subtitle inspection (possibly-forced flags,
removal of empty tracks). It also has the planner and the remuxers for
the lossless-audio supplements.

app.videos exports each name here again. Thus, the stored rq job
strings ("app.videos.mkvmerge_task") and the import sites continue to
resolve. The shared lock, subprocess, and copy code stays in
app.videos. The functions here import it lazily. Thus, the module
import direction stays one-way.
"""

import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import traceback

from datetime import datetime, timedelta, timezone
from functools import lru_cache

from pymediainfo import MediaInfo
from rq import get_current_job

from flask import current_app
from werkzeug.local import LocalProxy

from app.tasks import bulk_job_id, enqueue_task, report_progress, requeue
from app import db, get_app
from app.aws_storage import archive_library_file, mark_archive_stale
from app.models import File, FileAudioTrack, FileSubtitleTrack
from app.naming import hidden
from app.cache import cached_fetch
from app.settings import get_setting
from app.plex_library import enqueue_plex_analyze
from app.smb_probe import library_path, probe_and_record

# A staged remux holds the staged source and the output on the local
# disk at the same time (#298).

STAGED_REMUX_FACTOR = 2.2

# The free space that an in-place remux keeps beyond its second copy.

IN_PLACE_REMUX_MARGIN = 1024**3


def _on_volume_of(path, directory):
    """Return True if path is on the same volume as directory."""

    try:
        return os.stat(os.path.dirname(path)).st_dev == os.stat(directory).st_dev
    except OSError:
        return False


def _run_mkvmerge(command, job, name, activity):
    """Run an mkvmerge command, show its progress, and raise if it fails."""

    from app.videos import wait_for_subprocess

    current_app.logger.info(f"'{name}' Running mkvmerge: {command}")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    watch_mkvmerge_progress(process, job, name, activity)
    wait_for_subprocess(process, ok_returncodes=(0, 1))


# The tries of the staging reservation of a remux while its lock is busy.

REMUX_RESERVATION_TRIES = 3


def remux_file(file_path, build_command, job, name, activity, before_replace=None):
    """Remux a Matroska file and replace it with the result (#298).

    build_command(source, output) returns the mkvmerge command. A file on
    another volume (a library file on the NAS) is copied to staging. The
    remux runs on the local disk. The result is copied back to a hidden
    name and renamed over the file. Thus, the NAS sees 1 read and then 1
    write, and the file does not change until the rename. A stopped
    remux leaves its partial on staging.

    A file that is already on staging is remuxed in place. Without
    staging space, the remux also runs in place, as before.

    before_replace runs right before the rename over the file. A library
    rewrite uses it to mark the archive stale before the bytes change
    (#517)."""

    from app.importing import release_staging_reservation, reserve_staging
    from app.videos import copy_with_progress

    directory, basename = os.path.split(file_path)
    staging_dir = current_app.config["STAGING_DIR"]

    # The staging names hold a digest of the library path. The basename
    # of an extra is not unique. For example, many films have a
    # "Trailer.mkv", and 2 workers can remux 2 of them at the same time.

    staged_name = f"{hashlib.sha1(file_path.encode()).hexdigest()[:8]}-{basename}"
    reservation = f"remux:{staged_name}"
    staged = not _on_volume_of(file_path, staging_dir)
    if staged:
        # A busy reservation lock (an import burst) is no lack of space
        # (#569). The reservation tries again. Each try waits for the
        # lock first.
        for _ in range(REMUX_RESERVATION_TRIES):
            status = reserve_staging(
                reservation,
                int(os.path.getsize(file_path) * STAGED_REMUX_FACTOR),
                [f"src-{staged_name}", f"remux-{staged_name}"],
                staging_dir,
            )
            if status != "lock":
                break
        if status == "lock":
            # The scan, the audio plan and the post-reorder step have no
            # retry for this fault (#569 follow-up). Thus, the remux runs
            # in place after the tries, as it does for a lack of space.
            current_app.logger.warning(
                f"'{name}' The staging reservation lock stays busy. "
                f"Remuxing in place"
            )
            staged = False
        elif status != "ok":
            current_app.logger.warning(
                f"'{name}' Staging has no space for the remux. Remuxing in place"
            )
            staged = False

    if not staged:
        # The in-place remux writes a second copy next to the file. Check
        # the space first. A remux that fails with a full disk fills the
        # shared SSD to 0 bytes for a moment, and the other imports fail
        # (#262). A caller that waits for space retries.

        needed = os.path.getsize(file_path) + IN_PLACE_REMUX_MARGIN
        free = shutil.disk_usage(directory).free
        if free < needed:
            raise OSError(
                errno.ENOSPC,
                f"'{name}' needs {needed} bytes for an in-place remux, {free} are free",
            )
        hidden_output = hidden(os.path.join(directory, basename), ".remux.mkv")
        try:
            _run_mkvmerge(build_command(file_path, hidden_output), job, name, activity)
            if before_replace:
                before_replace()
            os.replace(hidden_output, file_path)
        except BaseException:
            if os.path.exists(hidden_output):
                os.remove(hidden_output)
            raise
        return

    staged_source = os.path.join(staging_dir, f".src-{staged_name}")
    staged_output = os.path.join(staging_dir, f".remux-{staged_name}")
    hidden_library = hidden(os.path.join(directory, basename))
    try:
        copy_with_progress(
            file_path, staged_source, job, name, "Copying to local staging"
        )
        _run_mkvmerge(build_command(staged_source, staged_output), job, name, activity)
        os.remove(staged_source)
        copy_with_progress(
            staged_output, hidden_library, job, name, "Moving into the library"
        )
        if before_replace:
            before_replace()
        os.replace(hidden_library, file_path)
    except BaseException:
        if os.path.exists(hidden_library):
            os.remove(hidden_library)
        raise
    finally:
        # The rename can be done when this cleanup runs. A fault here must
        # not hide that fact from the caller. Thus, a stray that stays
        # only logs a warning (#582)

        for path in (staged_source, staged_output):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                current_app.logger.warning(
                    f"'{name}' Cannot remove the staging copy {path}: {e}"
                )
        release_staging_reservation(reservation)


STOP_PROCESS_WAIT_SECONDS = 60


def stop_process(process):
    """Kill a child process that still runs, and wait for it to end.

    A task can fail during the read loop of a child. Examples are a job
    timeout and a Redis fault in report_progress. The child must not
    then write into an output that the cleanup deletes (#424)."""

    if process is not None and process.poll() is None:
        process.kill()

        # A child in uninterruptible NFS I/O ignores SIGKILL until the
        # I/O returns. The wait has a limit. Thus, the original fault
        # still goes up to the task handler.

        try:
            process.wait(timeout=STOP_PROCESS_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            current_app.logger.warning(
                f"Process {process.pid} did not end within "
                f"{STOP_PROCESS_WAIT_SECONDS} seconds of the kill"
            )


def watch_mkvmerge_progress(process, job, name, activity):
    """Stream the output of a process. Log its progress and update the job meta.

    A failure in the loop kills the process before it goes up."""

    previous_percent = None
    try:
        for line in process.stdout:
            progress_match = re.search(r"Progress\: \d+\%", line)
            if progress_match:
                progress_match = re.match(r"^Progress\: (?P<percent>\d+)\%", line)
                percent = int(progress_match.group("percent"))
                if previous_percent != percent:
                    current_app.logger.info(f"'{name}' {activity}: {percent}%")
                    previous_percent = percent
                report_progress(job, name, activity, percent)
    except BaseException:
        stop_process(process)
        raise


def parse_dolby_vision_profile(hdr_format):
    """Return the Dolby Vision variant ("5", "7", "8.1", ...) from the HDR-format string.

    The string is the combined HDR-format string of MediaInfo. Return
    None if the video is not DV. The profile number is in the
    codec-profile token (dvhe.08.06 is profile 8). dvhe and dvh1 are
    HEVC, dvav and dva1 are AVC, and dav1 is AV1. For profile 8, the
    meaningful variant is the cross-compatibility target. MediaInfo
    reports it as compatibility text in the same string. HDR10 is 8.1,
    HLG is 8.4, and plain SDR is 8.2.
    """

    if not hdr_format or "dolby vision" not in hdr_format.lower():
        return None
    text = hdr_format.lower()
    profile_match = re.search(r"(?:dv(?:he|h1|av|a1)|dav1)\.0?(\d{1,2})", text)
    if not profile_match:
        return None
    profile = int(profile_match.group(1))
    if profile == 8:
        if "hdr10" in text:
            return "8.1"
        if "hlg" in text:
            return "8.4"
        if "sdr" in text:
            return "8.2"
        return "8"
    return str(profile)


def _extract_media_details(file_path):
    """Parse a file and return the media details that its database records need.

    All data here is plain data: the video track fields, the audio and
    subtitle track dicts, and the file size. Thus, the sql-queue tasks
    that write the records never open the file.
    """

    media_info = MediaInfo.parse(file_path)
    current_app.logger.debug(
        f"'{os.path.basename(file_path)}' -> {media_info.to_json()}"
    )

    video = {}
    for track in media_info.tracks:
        if track.track_type == "Video" and track.format:
            video["format"] = track.format
            break

    for track in media_info.tracks:
        if track.track_type == "Video" and track.codec_id:
            video["codec"] = track.codec_id
            break

    for track in media_info.tracks:
        if track.track_type == "Video" and track.bit_rate:
            video["video_bitrate_kbps"] = track.bit_rate / 1000
            break

    # The HDR fields are always present. They are None when absent.
    # Thus, a new scan of a replaced file CLEARS the stale values. It
    # does not keep them

    video["hdr_format"] = None
    for track in media_info.tracks:
        if track.track_type == "Video" and track.other_hdr_format:
            if track.other_hdr_format[0]:
                video["hdr_format"] = track.other_hdr_format[0]
                break
    video["dolby_vision_profile"] = parse_dolby_vision_profile(video["hdr_format"])

    # The color tags tell an SDR file with BT.2020 colors (#292).

    video["color_primaries"] = None
    video["transfer_characteristics"] = None
    for track in media_info.tracks:
        if track.track_type == "Video":
            video["color_primaries"] = track.color_primaries
            video["transfer_characteristics"] = track.transfer_characteristics
            break

    return {
        "video": video,
        "audio_tracks": get_audio_tracks_from_file(file_path),
        "subtitle_tracks": get_subtitle_tracks_from_file(file_path),
        "filesize_bytes": os.path.getsize(file_path),
    }


def extract_track_metadata(file_path, file_id=None):
    """Remove the empty subtitle tracks and return the media details of a library file.

    This is the file half of a track scan. The returned details go to
    save_track_metadata on the sql queue. With file_id, the archive of
    that row is marked stale before the remux replaces the file (#517).
    """

    media_info = MediaInfo.parse(file_path)
    rewritten = False
    for track in media_info.tracks:
        if track.track_type == "General" and track.format == "Matroska":
            rewritten = remove_empty_subtitle_tracks(
                file_path,
                before_replace=(
                    (lambda: flag_archive_before_rewrite(file_id))
                    if file_id is not None
                    else None
                ),
            )
            break

    details = _extract_media_details(file_path)
    details["rewritten"] = bool(rewritten)
    return details


# The job id prefix of the per-file jobs of a library scan. It starts
# with the prefix of the scan run of the maintenance page (#439).

TRACK_SCAN_FILE_RUN = "bulk-track-scan-file-"


def track_metadata_scan_library():
    """Add all the files in the library to the metadata scan queue."""

    with app.app_context():
        try:

            # The ids let the scan button see a run that waits (#439).

            files = File.query.all()
            for file in files:
                enqueue_task(
                    "app.videos.track_metadata_scan_task",
                    file.id,
                    description=f"{file.basename} – Scanning track metadata",
                    job_id=bulk_job_id(TRACK_SCAN_FILE_RUN),
                )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            raise

        return True


def track_metadata_scan_task(file_id):
    """Scan the track metadata of a file from the file-operation queue.

    The file half runs here. It removes the empty subtitle tracks and
    parses the file. The extracted details go to save_track_metadata
    on the sql queue, together with the title lock. If a different
    task holds the title lock, this task tries again later.
    """

    with app.app_context():
        try:
            file = File.query.filter_by(id=file_id).first()
            if file is None:
                return False

            file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
            if not os.path.isfile(file_path):
                return True

            lock = current_app.lock_manager.lock(
                file.file_identifier(),
                current_app.config["MKVPROPEDIT_TASK_TIMEOUT"] * 1000,
            )
            if not lock:
                requeue(
                    "app.videos.track_metadata_scan_task",
                    key=int(file_id),
                    delay=(5, 15),
                    reason="Lock exists",
                    kwargs={"file_id": file_id},
                    description=f"'{file.basename}'",
                )
                return True

            try:
                details = extract_track_metadata(file_path, file_id)
            except Exception:
                current_app.lock_manager.unlock(lock)
                raise

            # The scan removed empty subtitle tracks. Thus, the library file
            # changed, and its archive must follow (#299). The save queues
            # the upload after its commit. Both write the same row. An
            # upload that starts first fails its commit with MariaDB error
            # 1020 (Oktapodi, 2026-10-24).

            archive = bool(details.pop("rewritten", False)) and get_setting(
                "ARCHIVE_ORIGINAL_MEDIA"
            )
            if archive:
                _flag_rewritten_archive(file_id)

            enqueue_task(
                "app.videos.save_track_metadata",
                file_id,
                details,
                lock,
                archive,
                description=f"{file.basename} – Saving track metadata",
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        return True


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


def _flag_rewritten_archive(file_id, strict=False):
    """Mark the archive of a rewritten library file as stale.

    The upload after the save clears the flag. If the save or the
    upload fails, the flag stays, and the weekly S3 sync uploads the
    file. The scan read the row before its long rewrite. Thus, a new
    transaction makes the change (MariaDB error 1020). After a scan,
    the rewrite is done, and a failed flag only logs. With strict, a
    failed flag raises after the rollback (#561)."""

    try:
        db.session.rollback()
        File.query.filter_by(id=file_id).update({"aws_stale": True})
        db.session.commit()
    except Exception:
        db.session.rollback()
        if strict:
            raise
        current_app.logger.warning(traceback.format_exc())


def flag_archive_before_rewrite(file_id):
    """Mark the archive stale right before a library rewrite (#517).

    A failure after the write and before the commit leaves the new bytes
    on disk and the old row. Without the flag, the weekly sync never
    uploads the file again, and a restore would bring back the older
    bytes. archive_library_file clears the flag after a good upload. The
    flag is set only when Fitzflix archives the library files.

    A failed flag raises (#561). This strict rule is for a caller that
    has not written yet. Then the rewrite stops and the library file
    stays as it was. The same database fault would fail the commit after
    the rewrite. A caller whose tool already wrote the file uses
    _flag_after_a_write (#584)."""

    if get_setting("ARCHIVE_ORIGINAL_MEDIA"):
        _flag_rewritten_archive(file_id, strict=True)


def _flag_after_a_write(file_id):
    """Mark the archive stale after a tool that can have written the file (#561).

    mkvpropedit writes the file in place. A kill or an error exit after
    its first write leaves new bytes on disk. A clean exit wrote them
    for sure. This is best effort (#584). After a failure, the caller
    raises the original error of the tool. After a success, the task
    goes on to the row commit and the upload at its end. A raise there
    skipped both and left the archive old with a clean flag."""

    try:
        flag_archive_before_rewrite(file_id)
    except Exception:
        current_app.logger.warning(traceback.format_exc())


def _rescan_after_a_failed_edit(file_id, name):
    """Queue a track scan of a file whose edit failed after the file changed (#583).

    The file has the new layout, but the track rows and the size keep
    the old layout. No periodic scan repairs them. The scan reads the
    rows again from the file. The job id is the id of the lock-busy
    retry of the scan. Thus, a repeat replaces the scheduled scan. This
    is best effort. The caller raises the original error."""

    try:
        requeue(
            "app.videos.track_metadata_scan_task",
            key=int(file_id),
            delay=(5, 15),
            reason="Track edit failed after the file changed",
            kwargs={"file_id": file_id},
            description=name,
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())


def save_track_metadata(file_id, details, lock=None, archive=False):
    """Write the extracted track metadata to the database.

    This is the sql half of a track scan. All work here is session
    work from the details dict. This function releases the given title
    lock when it is done. If archive is True, it queues the upload of
    the rewritten library file after its commit.
    """

    with app.app_context():
        try:
            file = File.query.filter_by(id=file_id).first()
            if file is None:
                return False

            # Set the video track info of the file

            file.date_updated = datetime.now(timezone.utc)
            for field, value in details["video"].items():
                setattr(file, field, value)

            record_filesize(file, details["filesize_bytes"])
            current_app.logger.info(f"{file} {file.filesize_bytes} bytes")

            replace_track_rows(
                file, details["audio_tracks"], details["subtitle_tracks"]
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        else:
            db.session.commit()
            if archive:
                enqueue_task(
                    "app.videos.upload_task", file_id, description=f"'{file.basename}'"
                )
            return True

        finally:
            if lock:
                current_app.lock_manager.unlock(lock)
                current_app.logger.info(f"Removed lock {lock}")


def track_metadata_scan(file_id):
    """Scan the metadata of a file again on demand.

    A different task can hold the lock of this title (for example, a
    remux, a property edit, or a transcode in progress). Then this
    function returns False without a scan.
    """

    file = File.query.filter_by(id=file_id).first()
    lock = current_app.lock_manager.lock(
        file.file_identifier(),
        current_app.config["MKVPROPEDIT_TASK_TIMEOUT"] * 1000,
    )
    if not lock:
        current_app.logger.warning(
            f"'{file.basename}' Lock exists, not rescanning track metadata"
        )
        return False

    try:
        details = extract_track_metadata(
            os.path.join(current_app.config["LIBRARY_DIR"], file.file_path), file_id
        )
    except Exception:
        current_app.lock_manager.unlock(lock)
        raise

    # A rewritten file needs its archive to follow (#299). The save
    # queues the upload after its commit.

    archive = bool(details.pop("rewritten", False)) and get_setting(
        "ARCHIVE_ORIGINAL_MEDIA"
    )
    if archive:
        _flag_rewritten_archive(file_id)
    return save_track_metadata(file_id, details, lock=lock, archive=archive)


# These are the languages that a track can have. Fitzflix reads them
# from the table of mkvtoolnix. Thus, the file page can never offer a
# code that the edit would reject. The keys are the ISO 639-2 codes.
# The track records store those codes. The other columns become
# aliases, not choices

FALLBACK_LANGUAGES = (
    ("eng", "English"),
    ("und", "Undetermined"),
    ("zxx", "No linguistic content"),
)

# ISO 639-2 gives 20 languages 2 codes. The bibliographic code is the
# only one that mkvtoolnix lists. The terminological code is what
# MediaInfo reports for some files ("deu" not "ger", "fra" not "fre").
# Both codes are the same language. Thus, the terminological spellings
# must resolve to the codes that the table knows. Fitzflix must not
# refuse them as unknown

ISO_639_2_TERMINOLOGIC = {
    "bod": "tib",
    "ces": "cze",
    "cym": "wel",
    "deu": "ger",
    "ell": "gre",
    "eus": "baq",
    "fas": "per",
    "fra": "fre",
    "hye": "arm",
    "isl": "ice",
    "kat": "geo",
    "mkd": "mac",
    "mri": "mao",
    "msa": "may",
    "mya": "bur",
    "nld": "dut",
    "ron": "rum",
    "slk": "slo",
    "sqi": "alb",
    "zho": "chi",
}


def language_key(code):
    """Return the bibliographic ISO 639-2 spelling of a language code.

    A terminological code ("deu", "fra") becomes its bibliographic code
    ("ger", "fre"). Any other value comes back in lower case. Thus, 2
    spellings of the same language give the same key (#423)."""

    code = str(code or "").strip().lower()
    return ISO_639_2_TERMINOLOGIC.get(code, code)


def same_language(first, second):
    """Return True if 2 language codes name the same language."""

    return bool(first) and bool(second) and language_key(first) == language_key(second)


def file_language_spelling(code, tracks):
    """Return the spelling of a language as the tracks of a file write it.

    MediaInfo reports the terminological code for some writers, and
    the bibliographic code for others. The mkvmerge language filters
    match the spelling in the file. This returns code if no track has
    the language."""

    for track in tracks:
        if same_language(track.get("language"), code):
            return track["language"]
    return code


@lru_cache(maxsize=1)
def _language_table():
    """Return the language table of mkvtoolnix as (name, 639-3, 639-2, 639-1) rows.

    The cache lasts for the life of the process. The table belongs to
    the installed mkvtoolnix, not to 1 file. The result is empty if
    Fitzflix cannot read the table. The callers then use a fallback.
    """

    try:
        listing = subprocess.run(
            [current_app.config["MKVMERGE_BIN"], "--list-languages"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    except Exception:
        current_app.logger.error(traceback.format_exc())
        return ()

    rows = []
    for line in listing.splitlines():
        # The format is "English language name | ISO 639-3 | ISO 639-2 |
        # ISO 639-1". The header and rule rows fail the 3-character check.
        # A 639-3-only language (Cantonese "yue", Mandarin "cmn") keeps an
        # empty 639-2 column. The dropdowns leave it out, but a track can
        # hold its code, and mkvpropedit sets it

        columns = [column.strip() for column in line.split("|")]
        if len(columns) < 4:
            continue
        if len(columns[2]) != 3 and not (not columns[2] and len(columns[1]) == 3):
            continue

        rows.append(tuple(columns[:4]))

    return tuple(rows)


@lru_cache(maxsize=1)
def iso_639_2_languages():
    """Return each (code, name) pair that a track can have, sorted by name."""

    rows = _language_table()
    if not rows:
        # Without the table, the page can still offer the 3 codes that
        # this feature exists to correct between

        return FALLBACK_LANGUAGES

    return tuple(
        sorted(
            ((iso_639_2, name) for name, _, iso_639_2, _ in rows if iso_639_2),
            key=lambda language: language[1].lower(),
        )
    )


@lru_cache(maxsize=1)
def _code_names():
    """Map each code that a track can hold to its name: 639-2, or 639-3 with no 639-2."""

    return {
        iso_639_2 or iso_639_3: name
        for name, iso_639_3, iso_639_2, _ in _language_table()
    }


@lru_cache(maxsize=1)
def _language_aliases():
    """Map each name of a language, in lowercase, to its code.

    The code is the 639-2 code, or the 639-3 code of a language that has
    no 639-2 code. The names are the language name, each code column in
    the table, and the ISO 639-2/T spellings that are not in the table.
    See ISO_639_2_TERMINOLOGIC.
    """

    aliases = {}

    # The 639-2 rows come first. Thus, a name that a 639-3-only language
    # shares maps to the 639-2 code. All codes come before all names.
    # Thus, the language named "Lou" (loj) does not take the code "lou"
    # of Louisiana Creole.

    rows = sorted(_language_table(), key=lambda row: not row[2])
    for name, iso_639_3, iso_639_2, iso_639_1 in rows:
        for alias in (iso_639_3, iso_639_2, iso_639_1):
            if alias:
                aliases.setdefault(alias.lower(), iso_639_2 or iso_639_3)
    for name, iso_639_3, iso_639_2, iso_639_1 in rows:
        if name:
            aliases.setdefault(name.lower(), iso_639_2 or iso_639_3)

    for terminologic, bibliographic in ISO_639_2_TERMINOLOGIC.items():
        if bibliographic in aliases:
            aliases.setdefault(terminologic, aliases[bibliographic])

    if not aliases:
        for code, name in FALLBACK_LANGUAGES:
            aliases[code] = code
            aliases[name.lower()] = code

    return aliases


@lru_cache(maxsize=1)
def language_names():
    """Map each code that a track record can hold to its display name.

    The keys are the aliases, not only the canonical codes. Thus, a
    track stored as "deu" still shows as German.
    """

    names = _code_names() or dict(iso_639_2_languages())
    return {
        alias: names[code]
        for alias, code in _language_aliases().items()
        if code in names
    }


# Fitzflix builds this again each hour, not on each render. A language
# that is new to the library appears in the dropdowns in that time

LANGUAGE_CHOICES_KEY = "fitzflix:language-choices"
LANGUAGE_CHOICES_SECONDS = 3600


def library_language_choices():
    """Return the languages to offer on the File page, as (code, name).

    The result has each ISO 639-2 language that also has a 639-1 code.
    That is 183 of the 1006 languages in the table. In practice, it is
    the set with a real publishing presence. The result also has each
    language that this collection uses outside that set.

    The whole table is approximately 54 KB of options per track. A
    select repeats its options for each track. Thus, the Doctor Who
    disc with 21 tracks would have 1 MB of options. The 639-1 set is
    8 KB per track. It still covers each film that a person could
    buy. It leaves out the long tail of dead languages and collective
    groups (Akkadian, "Algonquian languages"). No audio track is in
    those languages.

    The languages of the collection are always in the result. Thus, a
    track can never hold a language that the dropdown cannot show. und
    and zxx have no 639-1 code and would be missing without this. The
    list also grows when films in new languages arrive.
    """

    # Three DISTINCT queries over the track tables take approximately
    # 40 ms. That is too much for each File page render. The answer
    # changes only when a new language enters the library

    choices = cached_fetch(
        LANGUAGE_CHOICES_KEY, LANGUAGE_CHOICES_SECONDS, _stored_language_choices
    )
    return tuple(tuple(pair) for pair in choices)


def file_language_choices(stored_codes):
    """Return the File page choices, with the languages of the file itself (#545).

    The cached choices can predate a language that is new to the
    library. Then the dropdown of such a track selected nothing, and the
    browser posted the first option (Abkhazian) as a change. Thus, each
    resolved code of the file joins the choices."""

    choices = list(library_language_choices())
    offered = {code for code, _ in choices}
    names = _code_names() or dict(iso_639_2_languages())
    missing = {
        code
        for code in (resolve_language_code(value) for value in stored_codes)
        if code and code not in offered
    }
    if not missing:
        return tuple(choices)
    choices += [(code, names.get(code, code)) for code in missing]
    return tuple(sorted(choices, key=lambda language: language[1].lower()))


def _stored_language_choices():
    """Return the (code, name) choices of the library, for cached_fetch."""

    from app.models import FileAudioTrack, FileSubtitleTrack, Movie

    stored = set()
    for column in (
        FileAudioTrack.language,
        FileSubtitleTrack.language,
        Movie.tmdb_original_language,
    ):
        stored |= {value for (value,) in db.session.query(column).distinct() if value}

    codes = {resolve_language_code(value) for value in stored}
    codes |= {
        iso_639_2
        for name, iso_639_3, iso_639_2, iso_639_1 in _language_table()
        if iso_639_1 and iso_639_2
    }
    codes |= {
        "und",
        "zxx",
        resolve_language_code(current_app.config["NATIVE_LANGUAGE"]),
    }
    codes.discard(None)

    names = _code_names() or dict(iso_639_2_languages())
    return (
        sorted(
            ((code, names.get(code, code)) for code in codes),
            key=lambda language: language[1].lower(),
        ),
        True,
    )


def resolve_language_code(value):
    """Return the language code for the text typed in a language box.

    The code is ISO 639-2, or ISO 639-3 for a language without a 639-2
    code.

    The boxes hold language names. A bare code in one of the 3
    standards also works. The "German (ger)" pair also works. A person
    who types what they know must not have to guess the spelling that
    the table wants. Return None if nothing matches. The caller reports
    that. It does not guess.
    """

    value = (value or "").strip().lower()
    if not value:
        return None

    aliases = _language_aliases()
    if value in aliases:
        return aliases[value]

    paired = re.fullmatch(r"(?P<name>.+?)\s*\((?P<code>[a-z]{2,3})\)", value)
    if paired and paired.group("code") in aliases:
        return aliases[paired.group("code")]

    return None


def mkvpropedit_task(
    file_id,
    default_audio_track,
    default_subtitle_track,
    forced_subtitle_tracks,
    track_languages=None,
    transient_retries=0,
):
    """Update the MKV properties of a file.

    `track_languages` maps a track to the ISO 639-2 code to set. The
    keys are the track names of mkvpropedit ({"a1": "eng"}). None (or
    an empty mapping) changes no language.
    """

    # The shared lock, retry, and copy code stays in app.videos. The import
    # is lazy. Thus, the module import direction stays one-way

    from app.videos import (
        MAX_TRANSIENT_RETRIES,
        TRANSIENT_COPY_ERRNOS,
        acquire_lock_or_defer,
    )

    with app.app_context():
        file = File.query.filter_by(id=file_id).first()

        # Serialize with the other tasks that rewrite the files or the
        # track records of this title

        lock = acquire_lock_or_defer(
            file.file_identifier(),
            current_app.config["MKVPROPEDIT_TASK_TIMEOUT"] * 1000,
            current_app.file_queue,
            "app.videos.mkvpropedit_task",
            minutes=(5, 15),
            timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            args=(
                file_id,
                default_audio_track,
                default_subtitle_track,
                forced_subtitle_tracks,
                track_languages,
            ),
            kwargs={"transient_retries": transient_retries},
            key=int(file_id),
        )
        if not lock:
            return True

        # Bulk writes over SMB put the files into the lost-handle state.
        # Thus, probe this file directly at the end. Do not let the close
        # of a later upload find the problem

        probed_path = library_path(file)

        try:
            edited = mkvpropedit_unlocked(
                file_id,
                default_audio_track,
                default_subtitle_track,
                forced_subtitle_tracks,
                track_languages,
            )
        except OSError as e:
            probe_and_record(probed_path, context="mkvpropedit_task")
            if (
                e.errno in TRANSIENT_COPY_ERRNOS
                and transient_retries < MAX_TRANSIENT_RETRIES
                and not getattr(e, "retry_unsafe", False)
            ):
                # An unstable mount interrupted the edit before the file
                # was restructured. Thus, the same track arguments are
                # still valid. Try again after the mount is stable. The
                # finally block releases the lock. The retry takes the
                # lock again, like a new run

                requeue(
                    "app.videos.mkvpropedit_task",
                    key=int(file_id),
                    delay=timedelta(minutes=5),
                    reason=(
                        f"MKV property edit failed with a transient I/O error "
                        f"({e}) (attempt {transient_retries + 1} of "
                        f"{MAX_TRANSIENT_RETRIES})"
                    ),
                    counters={"transient_retries": transient_retries + 1},
                    args=(
                        file_id,
                        default_audio_track,
                        default_subtitle_track,
                        forced_subtitle_tracks,
                        track_languages,
                    ),
                    description=f"'{file.basename}'",
                )
                return False
            current_app.logger.error(traceback.format_exc())
            raise
        else:
            probe_and_record(probed_path, context="mkvpropedit_task")
            return edited
        finally:
            current_app.lock_manager.unlock(lock)


# The mkvmerge track order puts the default track of each type first,
# and the video tracks before the audio tracks before the subtitles.
# Thus, Plex selects the default audio track first

_TRACK_ORDER_TYPES = {"Video": 0, "Audio": 1, "Text": 2}
_TRACK_ORDER_DEFAULTS = {"Yes": 0, "No": 1}


def track_order(tracks):
    """Return the --track-order value of mkvmerge for the MediaInfo tracks.

    The order is video, then audio, then subtitles. In each type, the
    default track comes first. The sort is stable. Thus, 2 tracks of
    the same type and flag keep the order of the file."""

    kept = [
        track
        for track in tracks
        if track.track_type in _TRACK_ORDER_TYPES
        and track.default in _TRACK_ORDER_DEFAULTS
    ]
    kept.sort(
        key=lambda track: (
            _TRACK_ORDER_TYPES[track.track_type],
            _TRACK_ORDER_DEFAULTS[track.default],
        )
    )
    return ",".join(f"0:{track.streamorder}" for track in kept)


def build_propedit_args(
    audio_count,
    subtitle_count,
    default_audio_track,
    default_subtitle_track,
    forced_subtitle_tracks,
    track_languages=None,
):
    """Return the mkvpropedit arguments that set the flags and the languages.

    The track ids are strings that count from 1 in each type. Each
    audio track gets its default flag. The subtitle tracks get their
    default and forced flags only when the caller selected a default
    or a forced track. The language corrections are independent: a
    file whose flags are correct still needs them. mkvpropedit also
    applies a language to LanguageIETF."""

    args = []
    for track_id in range(1, audio_count + 1):
        flag = 1 if str(track_id) == default_audio_track else 0
        args += ["--edit", f"track:a{track_id}", "--set", f"flag-default={flag}"]
    if default_subtitle_track or forced_subtitle_tracks:
        for track_id in range(1, subtitle_count + 1):
            default = 1 if str(track_id) == default_subtitle_track else 0
            forced = 1 if str(track_id) in forced_subtitle_tracks else 0
            args += ["--edit", f"track:s{track_id}", "--set", f"flag-default={default}"]
            args += ["--edit", f"track:s{track_id}", "--set", f"flag-forced={forced}"]
    for track_type, count in (("a", audio_count), ("s", subtitle_count)):
        for track_id in range(1, count + 1):
            language = (track_languages or {}).get(f"{track_type}{track_id}")
            if language:
                args += [
                    "--edit",
                    f"track:{track_type}{track_id}",
                    "--set",
                    f"language={language}",
                ]
    return args


def mkvpropedit_unlocked(
    file_id,
    default_audio_track,
    default_subtitle_track,
    forced_subtitle_tracks,
    track_languages=None,
):
    """Update the MKV properties of a file. The caller must hold the lock of the title."""

    from app.videos import wait_for_subprocess

    with app.app_context():
        # After the reorder remux or the empty-subtitle removal is renamed
        # into place, the track numbering of the file is different. Thus,
        # a retry with the original track arguments of the caller would
        # flag the wrong tracks (#582)

        restructured = False

        # After mkvpropedit starts, the file can differ from the track
        # rows until the commit. A failure in that window queues a scan
        # (#583)

        changed = False
        name = f"File {file_id}"

        try:
            job = get_current_job()

            # Get the record of the file to modify

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            name = f"'{file.basename}'"

            report_progress(job, file.basename, "Updating MKV properties")

            audio_tracks = get_audio_tracks_from_file(file_path)
            subtitle_tracks = get_subtitle_tracks_from_file(file_path)

            current_app.logger.info(f"{file.basename} file_id: {file_id}")
            current_app.logger.info(
                f"{file.basename} selected default_audio_track: {default_audio_track} {type(default_audio_track)}"
            )
            current_app.logger.info(
                f"{file.basename} selected default_subtitle_track: {default_subtitle_track} {type(default_subtitle_track)}"
            )
            current_app.logger.info(
                f"{file.basename} selected forced_subtitle_tracks: {forced_subtitle_tracks} {type(forced_subtitle_tracks)}"
            )
            current_app.logger.info(
                f"{file.basename} selected track_languages: {track_languages} {type(track_languages)}"
            )

            # The web form sends the track ids as strings. mkvmerge_task
            # sends ints. None means that the file has no audio tracks for a
            # default. Normalize them 1 time. Thus, each comparison below
            # compares the same types.

            if default_audio_track is not None:
                default_audio_track = str(default_audio_track)
            if default_subtitle_track is not None:
                default_subtitle_track = str(default_subtitle_track)

            localization_arguments = build_propedit_args(
                len(audio_tracks),
                len(subtitle_tracks),
                default_audio_track,
                default_subtitle_track,
                forced_subtitle_tracks,
                track_languages,
            )

            current_app.logger.info(
                f"{file.basename} localization_arguments: {localization_arguments}"
            )

            if localization_arguments:

                mkvpropedit_task = subprocess.Popen(
                    [
                        current_app.config["MKVPROPEDIT_BIN"],
                        file_path,
                    ]
                    + localization_arguments,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1,
                )
                changed = True
                try:
                    try:
                        for line in mkvpropedit_task.stdout:
                            line = line.replace("\n", "")
                            current_app.logger.info(f"'{file.basename}' {line}")
                    except BaseException:
                        # The child must not keep writing after a fault (#499).
                        stop_process(mkvpropedit_task)
                        raise

                    wait_for_subprocess(mkvpropedit_task, ok_returncodes=(0, 1))
                except BaseException:
                    # The tool ran. It can have written the file before the
                    # kill or the error exit (#561). Thus, the archive is
                    # marked stale in this case too.

                    _flag_after_a_write(file_id)
                    raise

                # mkvpropedit writes the file in place (#517). The flag goes
                # on after the tool reports success (follow-up #517), or
                # after a failure once the tool ran (#561). A tool that
                # could not start leaves the row clean. A stale row refuses
                # a direct restore and costs an upload. The write is done.
                # Thus, a failed flag only logs (#584).

                _flag_after_a_write(file_id)

                # If the default audio track is not the first track, make a
                # new file with the default audio track first. Then Plex
                # selects it first

                if default_audio_track is not None and default_audio_track != "1":
                    new_track_order = track_order(MediaInfo.parse(file_path).tracks)

                    current_app.logger.info(
                        f"{file.basename} new_track_order: {new_track_order}"
                    )

                    # The remux runs on staging and replaces the file at
                    # the end (#298).

                    remux_file(
                        file_path,
                        lambda source, output: [
                            current_app.config["MKVMERGE_BIN"],
                            "--track-order",
                            new_track_order,
                            "-o",
                            output,
                            source,
                        ],
                        job,
                        file.basename,
                        "Remuxing",
                        before_replace=lambda: flag_archive_before_rewrite(file_id),
                    )
                    restructured = True

            # Remove the subtitle tracks that have zero elements. The
            # removal renumbers the subtitle tracks that stay (#582)

            if remove_empty_subtitle_tracks(
                file_path, before_replace=lambda: flag_archive_before_rewrite(file_id)
            ):
                restructured = changed = True

            # Build the audio and subtitle track info again after the changes

            output_audio_tracks = get_audio_tracks_from_file(file_path)
            output_subtitle_tracks = get_subtitle_tracks_from_file(file_path)

            # The file work can take 30 minutes (a remux). A transaction
            # from before it fails its writes with MariaDB error 1020 if a
            # triage click or an upload changed the row. A delete of the
            # track rows before it held their locks for the whole remux,
            # and a new import waited past the lock timeout. Thus, the
            # writes start a new transaction here.

            file = _fresh_file(file.id)
            replace_track_rows(file, output_audio_tracks, output_subtitle_tracks)

            record_filesize(file, os.path.getsize(file_path))
            file.date_updated = datetime.now(timezone.utc)
            db.session.commit()

        except OSError as e:
            # Tell the caller if a retry with the same arguments is still
            # safe. The log entry (or a quiet transient defer) is the job of
            # the caller

            e.retry_unsafe = restructured
            db.session.rollback()
            if changed:
                _rescan_after_a_failed_edit(file_id, name)
            raise

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            if changed:
                _rescan_after_a_failed_edit(file_id, name)
            raise

        else:
            # The file on disk changed. The default flags changed. After a
            # reorder, the whole track layout changed. Plex reads it again
            # now, not at its next scan

            enqueue_plex_analyze(file_path)

        # The edited library file goes to modified/, or to transcoded/
        # for a conversion (#299). The rip in untouched/ never changes.

        if get_setting("ARCHIVE_ORIGINAL_MEDIA"):
            try:
                archive_library_file(file, file_path)

            except OSError as e:
                # The edit already succeeded and committed. Thus, a retry
                # of the whole task could edit a restructured file again.
                # Only the second upload was lost.
                #
                # No other process can find that loss. The S3 key still
                # exists. It has the date of the previous upload. Thus,
                # the sync task reads the row as consistent. Then it fills
                # the recorded size from the stale object. This makes the
                # row consistent with the wrong copy. Without a repair,
                # the archive stays a pre-edit file for ever. Thus, record
                # the problem explicitly. The repair queue handles it.

                e.retry_unsafe = True
                current_app.logger.error(traceback.format_exc())
                db.session.rollback()
                mark_archive_stale(file_id, reason="re-archive after a track edit")
                raise

            except Exception:
                current_app.logger.error(traceback.format_exc())
                db.session.rollback()
                mark_archive_stale(file_id, reason="re-archive after a track edit")
                raise

            else:
                db.session.commit()

        return True


def mkvmerge_task(file_id, audio_tracks, subtitle_tracks):
    """Remux an MKV file."""

    from app.videos import acquire_lock_or_defer

    with app.app_context():
        file = File.query.filter_by(id=file_id).first()

        # Serialize with the other tasks that rewrite the files or the
        # track records of this title

        lock = acquire_lock_or_defer(
            file.file_identifier(),
            current_app.config["MKVPROPEDIT_TASK_TIMEOUT"] * 1000,
            current_app.import_queue,
            "app.videos.mkvmerge_task",
            minutes=(5, 15),
            timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            args=(file_id, audio_tracks, subtitle_tracks),
            key=int(file_id),
        )
        if not lock:
            return True

        try:
            return mkvmerge_unlocked(file_id, audio_tracks, subtitle_tracks)
        finally:
            current_app.lock_manager.unlock(lock)


def mkvmerge_unlocked(file_id, audio_tracks, subtitle_tracks):
    """Remux an MKV file. The caller must hold the lock of the title."""

    with app.app_context():
        # After the remux replaces the file, the file differs from the
        # track rows until the commit. A failure in that window queues a
        # scan (#583)

        changed = False
        name = f"File {file_id}"

        try:
            job = get_current_job()

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            name = f"'{file.basename}'"

            # The form gives the 1-based position of each track in its
            # kind (FileAudioTrack.track). mkvmerge wants the container
            # stream order. MediaInfo lists the tracks grouped by kind,
            # so its list index is the stream order only when the file has
            # video, then audio, then subtitles. Each track carries its own
            # stream order. remove_empty_subtitle_tracks uses it the same
            # way (#315).

            media_info = MediaInfo.parse(file_path)
            stream_orders = {"Audio": [], "Text": []}
            for track in media_info.tracks:
                if track.track_type in stream_orders:
                    stream_orders[track.track_type].append(
                        track.to_data().get("streamorder")
                    )
            current_app.logger.info(f"Stream orders: {stream_orders}")
            current_app.logger.info(f"Audio tracks: {audio_tracks}")
            current_app.logger.info(f"Subtitle tracks: {subtitle_tracks}")

            def stream_ids(kind, positions):
                """Return the mkvmerge ids of the tracks at the form positions."""

                orders = stream_orders[kind]
                ids = []
                for position in positions:
                    index = int(position) - 1
                    if not 0 <= index < len(orders) or not str(orders[index]).isdigit():
                        raise RuntimeError(
                            f"'{file.basename}' has no stream order for "
                            f"{kind} track {position}. The remux stops."
                        )
                    ids.append(int(orders[index]))
                return ids

            audio_tracks = stream_ids("Audio", audio_tracks)
            subtitle_tracks = stream_ids("Text", subtitle_tracks)

            current_app.logger.info(f"Modified audio tracks: {audio_tracks}")
            current_app.logger.info(f"Modified subtitle tracks: {subtitle_tracks}")

            track_args = []
            if len(audio_tracks) >= 1:
                track_args.extend(["-a", ",".join(map(str, audio_tracks))])
            else:
                track_args.append("--no-audio")
            if len(subtitle_tracks) >= 1:
                track_args.extend(["-s", ",".join(map(str, subtitle_tracks))])
            else:
                track_args.append("--no-subtitles")

            # The remux runs on staging and replaces the file at the end
            # (#298).

            remux_file(
                file_path,
                lambda source, output: [
                    current_app.config["MKVMERGE_BIN"],
                    "-o",
                    output,
                    "--title",
                    "",
                    "--track-name",
                    "-1:",
                ]
                + track_args
                + [source],
                job,
                file.basename,
                "Remuxing",
                before_replace=lambda: flag_archive_before_rewrite(file_id),
            )
            changed = True

            # Remove the subtitle tracks that have zero elements

            remove_empty_subtitle_tracks(
                file_path, before_replace=lambda: flag_archive_before_rewrite(file_id)
            )

            # Build the audio and subtitle track info again after the changes

            output_audio_tracks = get_audio_tracks_from_file(file_path)
            output_subtitle_tracks = get_subtitle_tracks_from_file(file_path)

            # The file work can take 30 minutes (a remux). A transaction
            # from before it fails its writes with MariaDB error 1020 if a
            # triage click or an upload changed the row. A delete of the
            # track rows before it held their locks for the whole remux,
            # and a new import waited past the lock timeout. Thus, the
            # writes start a new transaction here.

            file = _fresh_file(file.id)
            replace_track_rows(file, output_audio_tracks, output_subtitle_tracks)
            db.session.commit()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            if changed:
                _rescan_after_a_failed_edit(file_id, name)
            raise

        else:
            # This task already holds the lock of the title. Thus, call the
            # unlocked variant directly. A call to the locked variant would
            # deadlock against this task. An OSError from the unlocked
            # variant leaves the log entry to the caller. Thus, log it here

            try:
                mkvpropedit_unlocked(file.id, 1, None, None)
            except OSError:
                current_app.logger.error(traceback.format_exc())
                raise
            return True


def get_audio_tracks_from_file(file_path):
    """Parse a file with MediaInfo and return its audio tracks."""

    audio_tracks = []
    media_info = MediaInfo.parse(file_path)
    current_app.logger.debug(f"{os.path.basename(file_path)} -> {media_info.to_json()}")

    for track in media_info.tracks:
        if track.track_type == "Audio":
            audio_track = {}
            language = track.to_data().get("other_language", "und")

            if language == "und":
                audio_track["language"] = "und"
                audio_track["language_name"] = "Undetermined"

            elif "zxx" in language:
                audio_track["language"] = "zxx"
                audio_track["language_name"] = "Not applicable"

            elif len(language) >= 4:
                audio_track["language"] = language[3]
                audio_track["language_name"] = language[0]

            else:
                # A language with no 2-letter code (Cantonese "yue",
                # Mandarin "cmn", Filipino "fil") has only 3 elements. The
                # 3-letter code is among them. As "und", the track did not
                # match its own language. The import dropped it, and a film
                # with only that audio came in silent.

                audio_track["language"] = "und"
                audio_track["language_name"] = "Undetermined"
                for element in language:
                    if len(element) == 3:
                        audio_track["language"] = element
                        audio_track["language_name"] = language[0]
                        break

            audio_track["streamorder"] = (
                int(track.to_data().get("streamorder"))
                if str(track.to_data().get("streamorder", "")).isdigit()
                else None
            )
            audio_track["format"] = track.to_data().get("format")

            audio_track["channels"] = (
                float(track.to_data().get("channel_s"))
                if str(track.to_data().get("channel_s", "")).isdigit()
                else None
            )

            # Change the channel layout of the track to include the LFE
            # channel if present. Keep None if MediaInfo did not report a
            # usable channel count
            if audio_track["channels"] and "LFE" in track.to_data().get(
                "channel_layout", ""
            ):
                audio_track["channels"] = str(audio_track["channels"] - 1 + 0.1)
            elif audio_track["channels"] is not None:
                audio_track["channels"] = str(audio_track["channels"] * 1.0)

            audio_track["default"] = (
                True if track.to_data().get("default") == "Yes" else False
            )
            audio_track["codec"] = track.to_data().get("commercial_name")
            audio_track["bitrate"] = (
                int(track.to_data().get("bit_rate"))
                if str(track.to_data().get("bit_rate", "")).isdigit()
                else None
            )
            audio_track["bitrate_kbps"] = (
                round(int(track.to_data().get("bit_rate")) / 1000)
                if str(track.to_data().get("bit_rate", "")).isdigit()
                else None
            )
            audio_track["bit_depth"] = (
                int(track.to_data().get("bit_depth"))
                if str(track.to_data().get("bit_depth", "")).isdigit()
                else None
            )
            audio_track["sampling_rate"] = (
                int(track.to_data().get("sampling_rate"))
                if str(track.to_data().get("sampling_rate", "")).isdigit()
                else None
            )
            audio_track["sampling_rate_khz"] = (
                int(int(track.to_data().get("sampling_rate")) / 1000)
                if str(track.to_data().get("sampling_rate", "")).isdigit()
                else None
            )
            audio_track["compression_mode"] = track.to_data().get("compression_mode")
            if (
                audio_track["compression_mode"] is None
                and audio_track["codec"] == "PCM"
            ):
                audio_track["compression_mode"] = "Lossless"

            audio_tracks.append(audio_track)

    current_app.logger.info(
        f"'{os.path.basename(file_path)}' Audio tracks: {audio_tracks}"
    )
    return audio_tracks


def get_subtitle_tracks_from_file(file_path):
    """Parse a file with MediaInfo and return its subtitle tracks."""

    subtitle_tracks = []
    media_info = MediaInfo.parse(file_path)
    current_app.logger.debug(f"{os.path.basename(file_path)} -> {media_info.to_json()}")

    for track in media_info.tracks:
        if track.track_type == "Text":
            subtitle_track = {}
            language = track.to_data().get("other_language", "und")

            if language == "und" or len(language) == 0:
                subtitle_track["language"] = "und"
                subtitle_track["language_name"] = "Undetermined"

            elif "zxx" in language:
                subtitle_track["language"] = "zxx"
                subtitle_track["language_name"] = "Not applicable"

            elif len(language) <= 3:
                # The 3-character language code is usually in the 4th
                # position of the other_language variable. But sometimes the
                # other_language variable has only 3 elements. If
                # other_language has no 4th element, set the default to
                # "Undetermined" / "und". Then look for a value that is 3
                # characters long. Use that value if it exists.

                subtitle_track["language"] = "und"
                subtitle_track["language_name"] = "Undetermined"

                for l in language:
                    if len(l) == 3:
                        subtitle_track["language"] = l
                        subtitle_track["language_name"] = language[0]
                        break

            else:
                subtitle_track["language"] = language[3]
                subtitle_track["language_name"] = language[0]

            subtitle_track["streamorder"] = (
                int(track.to_data().get("streamorder"))
                if str(track.to_data().get("streamorder", "")).isdigit()
                else None
            )
            subtitle_track["elements"] = int(
                track.to_data().get("count_of_elements", 0)
            )
            subtitle_track["default"] = (
                True if track.to_data().get("default") == "Yes" else False
            )
            subtitle_track["forced"] = (
                True if track.to_data().get("forced") == "Yes" else False
            )
            subtitle_track["format"] = track.to_data().get("format")
            subtitle_tracks.append(subtitle_track)

    current_app.logger.info(
        f"'{os.path.basename(file_path)}' Subtitle tracks: {subtitle_tracks}"
    )
    return subtitle_tracks


def replace_track_rows(file, audio_tracks, subtitle_tracks):
    """Replace the audio and subtitle rows of a file with the given track dicts.

    This is the one write path of the track rows: the import, a rescan,
    the in-place rewrites and the Atmos supplement. The old rows go in a
    delete. Each track dict gets the file id and a 1-based track number.
    The forced-subtitle pass runs on every write. It marks the forced
    state of a suspicious subtitle track as unknown (None) before the
    insert. Return the new FileAudioTrack rows in track order."""

    FileAudioTrack.query.filter_by(file_id=file.id).delete()
    FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

    audio_rows = []
    for i, track in enumerate(audio_tracks):
        track["file_id"] = file.id
        track["track"] = i + 1
        audio_track = FileAudioTrack(**track)
        current_app.logger.info(f"{file} Adding audio track {audio_track}")
        db.session.add(audio_track)
        audio_rows.append(audio_track)

    flag_possibly_forced_subtitles(file, subtitle_tracks)
    for i, track in enumerate(subtitle_tracks):
        track["file_id"] = file.id
        track["track"] = i + 1
        subtitle_track = FileSubtitleTrack(**track)
        current_app.logger.info(f"{file} Adding subtitle track {subtitle_track}")
        db.session.add(subtitle_track)
    return audio_rows


def flag_possibly_forced_subtitles(file, subtitle_tracks):
    """Find the subtitle tracks that can be forced subtitle tracks.

    A track qualifies if it has elements, but not more than 1/3 of the
    elements of the first subtitle track. The track must not be marked
    as forced. This function sets the forced flag of that track to unknown
    (None). It reports that the file can have a forced subtitle track.
    """

    possibly_forced_subtitle = False

    if len(subtitle_tracks) > 1:
        main_subtitle_track = subtitle_tracks[0].get("elements")
        for i, track in enumerate(subtitle_tracks[1:]):
            track_length = track.get("elements")
            forced_flag = track.get("forced")

            if (
                main_subtitle_track
                and track_length
                and track_length <= (main_subtitle_track * 0.3)
                and not forced_flag
            ):
                current_app.logger.warning(
                    f"{file} Subtitle track {i+2} has {track_length} elements "
                    f"and may be a forced subtitle track!"
                )
                subtitle_tracks[i + 1]["forced"] = None
                possibly_forced_subtitle = True

    return possibly_forced_subtitle


def remove_empty_subtitle_tracks(file_path, before_replace=None):
    """Remux a Matroska file in place to remove the subtitle tracks with zero elements.

    This function removes only the tracks whose statistics tags report
    zero elements. It does not change a track with no statistics.
    Fitzflix cannot know if that track is empty.

    Return True if the function rewrote the file. Return False if there
    was nothing to remove. before_replace goes to remux_file.
    """

    basename = os.path.basename(file_path)
    job = get_current_job()
    media_info = MediaInfo.parse(file_path)

    keep_track_ids = []
    empty_track_ids = []
    for track in media_info.tracks:
        if track.track_type != "Text":
            continue

        streamorder = track.to_data().get("streamorder")
        elements = track.to_data().get("count_of_elements")

        # The mkvmerge track id (the stream order) selects the tracks.
        # Thus, a remux is not safe if an id is unknown

        if not str(streamorder).isdigit():
            current_app.logger.warning(
                f"'{basename}' Subtitle track ids are unknown, "
                f"skipping empty-subtitle-track removal"
            )
            return False

        if str(elements).isdigit() and int(elements) == 0:
            empty_track_ids.append(int(streamorder))
        else:
            keep_track_ids.append(int(streamorder))

    if not empty_track_ids:
        return False

    current_app.logger.info(
        f"'{basename}' Removing zero-element subtitle track id(s) {empty_track_ids}"
    )

    subtitle_args = (
        ["-s", ",".join(str(id) for id in keep_track_ids)]
        if keep_track_ids
        else ["--no-subtitles"]
    )

    # A library file is remuxed on staging. A file that is already on
    # staging (the import) is remuxed in place (#298).

    remux_file(
        file_path,
        lambda source, output: [current_app.config["MKVMERGE_BIN"], "-o", output]
        + subtitle_args
        + [source],
        job,
        basename,
        "Removing empty subtitle tracks",
        before_replace=before_replace,
    )
    return True


def plan_audio_supplements(audio_tracks):
    """Return the output audio-track order for the supplement pass.

    The result is a list of (action, source index) pairs. The action
    "flac" converts that source track. The action "copy" keeps it.

    Each lossless track that is not already FLAC or PCM gets a FLAC
    twin immediately before it. This mirrors the MakeMKV rip profile
    "FLAC Plus Original Audio". The original always stays. A FLAC
    counts as an existing twin ONLY if it is immediately before a
    lossless track in the same language. That is the exact shape that
    the rip profile makes. A FLAC in a different position can be
    anything (for example, a commentary). Thus, it never counts as a
    twin. Fitzflix never moves it, and never gives it the default slot
    (rule from Glenn). Its neighbor gets a new converted twin instead.
    By design, the channel counts do NOT have to match. MediaInfo
    labels the DTS-ES Matrix sources "6.0". But their discrete
    content, and thus each lossless FLAC decode of them, is 5.1 (the
    LOTR discs). A strict channel match would call the correct twins
    imperfect and add redundant twins. Files that already have the
    twin shape plan as pure copies. Thus, the pass is idempotent
    across disc rips and S3 downloads.
    """

    plan = []
    for index, track in enumerate(audio_tracks):
        if track.get("compression_mode") == "Lossless" and track.get("format") not in [
            "FLAC",
            "PCM",
        ]:
            previous = audio_tracks[index - 1] if index > 0 else None
            twinned = (
                previous is not None
                and previous.get("format") == "FLAC"
                and previous.get("language") == track.get("language")
            )
            if not twinned:
                plan.append(("flac", index))
        plan.append(("copy", index))
    return plan


def copy_image_attachments(source, output):
    """Copy the image attachments (cover art) of a Matroska file to an output.

    ffmpeg shows an image attachment as an attached picture, not as an
    attachment stream. The "0:V" map leaves it out, and "0:t?" does not
    carry it. Thus, mkvpropedit adds each image to the ffmpeg output
    again, in place. This returns the number of images that it copied.
    """

    def run(command):
        """Run an mkvtoolnix command. Exit code 1 is only a warning."""

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode > 1:
            raise RuntimeError(
                f"{os.path.basename(command[0])} failed: "
                f"{(result.stdout + result.stderr)[-500:]}"
            )
        return result.stdout

    identity = json.loads(run([current_app.config["MKVMERGE_BIN"], "-J", source]))
    images = [
        attachment
        for attachment in identity.get("attachments") or []
        if (attachment.get("content_type") or "").startswith("image/")
    ]
    if not images:
        return 0

    with tempfile.TemporaryDirectory(
        prefix=".attachments-", dir=os.path.dirname(output)
    ) as scratch:
        extracted = {
            attachment["id"]: os.path.join(scratch, str(attachment["id"]))
            for attachment in images
        }
        run(
            [current_app.config["MKVEXTRACT_BIN"], source, "attachments"]
            + [f"{number}:{path}" for number, path in extracted.items()]
        )
        command = [current_app.config["MKVPROPEDIT_BIN"], output]
        for attachment in images:
            command += [
                "--attachment-name",
                attachment.get("file_name") or f"cover-{attachment['id']}",
                "--attachment-mime-type",
                attachment["content_type"],
            ]
            if attachment.get("description"):
                command += ["--attachment-description", attachment["description"]]
            command += ["--add-attachment", extracted[attachment["id"]]]
        run(command)
    return len(images)


def build_supplement_args(plan):
    """Return the ffmpeg audio arguments for a supplement plan.

    The codec and disposition options are numbered by OUTPUT position.
    A source track mapped 2 times (converted twin plus original) moves
    each later output index. Thus, the input index appears only in the
    -map selector. The first output track is the default. All other
    tracks are cleared. This matches the convention of the rip profile
    that the natively playable track leads.
    """

    args = []
    for output_index, (action, source_index) in enumerate(plan):
        args.extend(
            [
                "-map",
                f"0:a:{source_index}",
                f"-c:a:{output_index}",
                "flac" if action == "flac" else "copy",
            ]
        )
    for output_index in range(len(plan)):
        args.extend(
            [
                f"-disposition:a:{output_index}",
                "default" if output_index == 0 else "none",
            ]
        )
    return args


# The time stamp of an ffmpeg progress line, for example "time=01:02:03".

FFMPEG_TIME_RE = re.compile(
    r"time\=(?P<hour>\d{2})\:(?P<minute>\d{2}):(?P<seconds>\d{2})"
)


def ffmpeg_progress(line, duration, previous):
    """Return the percent of an ffmpeg progress line, or previous.

    The percent stops at 99 (#388). A stream can run past the duration
    in the container header. The SDR conversion stops at 99 the same
    way. A line with no time stamp, or no duration, gives previous."""

    match = FFMPEG_TIME_RE.search(line)
    if not match or not duration:
        return previous
    elapsed = (
        int(match.group("hour")) * 3600
        + int(match.group("minute")) * 60
        + int(match.group("seconds"))
    )
    return min(99, int(elapsed / duration * 100))


def supplement_lossless_tracks(file_path, file_id=None):
    """Give each lossless non-FLAC/PCM audio track a FLAC twin before it.

    The original stays. The twin plays natively on Apple TV clients.
    The lossless original stays for direct play and future
    passthrough. Some files already have their twins. These are MakeMKV
    rips made with the "FLAC Plus Original Audio" profile, or downloads
    of uploads that already had supplements. These files plan as pure
    copies, and the pass does not change them. Thus, the pass is safe
    to repeat.
    """

    from app.videos import wait_for_subprocess

    with app.app_context():
        temp_flac_file = None
        flac_track_process = None
        try:
            job = get_current_job()

            dirname = os.path.dirname(file_path)
            basename = os.path.basename(file_path)

            audio_tracks = get_audio_tracks_from_file(file_path)
            if not audio_tracks:
                return True

            plan = plan_audio_supplements(audio_tracks)
            conversions = sum(1 for action, _ in plan if action == "flac")
            if not conversions:
                current_app.logger.info(
                    f"'{basename}' All lossless tracks already have FLAC twins"
                )
                return True

            current_app.logger.info(f"'{basename}' Parsing with MediaInfo")
            media_info = MediaInfo.parse(file_path)
            current_app.logger.debug(f"'{basename}' -> {media_info.to_json()}")

            container = None
            file_duration = 0
            for track in media_info.tracks:
                if track.track_type == "General" and track.format:
                    container = track.format
                    current_app.logger.info(f"'{basename}' File container {container}")

                    # Convert the file duration from milliseconds to
                    # seconds. A truncated Matroska has no duration. 0
                    # means no progress percentage.
                    file_duration = int(track.duration) / 1000 if track.duration else 0
                    current_app.logger.info(f"'{basename}' Duration: {file_duration}s")

            if container != "Matroska":
                current_app.logger.warning(
                    f"'{basename}' Unable to supplement lossless tracks as is not a MKV file!"
                )
                return False

            current_app.logger.info(
                f"'{basename}' Supplementing {conversions} lossless "
                f"track{'s' if conversions != 1 else ''} with FLAC"
            )
            audio_args = build_supplement_args(plan)
            current_app.logger.info(f"Audio map: {audio_args}")
            temp_flac_file = f"{dirname}/.{basename}"

            flac_track_process = subprocess.Popen(
                [
                    current_app.config["FFMPEG_BIN"],
                    "-y",
                    "-i",
                    file_path,
                    # "V" is each video track, but not an attached
                    # picture. ffmpeg shows a cover-art attachment as a
                    # video stream. With "v", it became a second video
                    # track. copy_image_attachments adds the cover again.
                    "-map",
                    "0:V",
                    "-c:v",
                    "copy",
                ]
                + audio_args
                + [
                    "-map",
                    "0:s:?",
                    "-c:s",
                    "copy",
                    # The attachments: the fonts of styled ASS subtitles.
                    # Without this map, ffmpeg dropped them.
                    "-map",
                    "0:t?",
                    "-c:t",
                    "copy",
                    temp_flac_file,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )
            progress = 0
            previous_percent = None
            for line in flac_track_process.stdout:
                progress = ffmpeg_progress(line, file_duration, progress)
                if previous_percent != progress:
                    current_app.logger.info(
                        f"'{basename}' Supplementing lossless tracks with FLAC: {progress}%"
                    )
                    previous_percent = progress
                report_progress(
                    job, basename, "Supplementing lossless tracks with FLAC", progress
                )

            wait_for_subprocess(flac_track_process)
            copy_image_attachments(file_path, temp_flac_file)

            current_app.logger.info(f"'{basename}' Supplemented lossless tracks")
            current_app.logger.info(f"Moving '{temp_flac_file}' to '{file_path}'")
            shutil.move(temp_flac_file, file_path)

            if file_id:
                track_metadata_scan_task(file_id)

        except BaseException:
            current_app.logger.error(traceback.format_exc())

            # The partial output is as large as the file. The cleanup of the
            # import does not know its name. A job timeout leaves ffmpeg
            # writing, so stop it first.

            if flac_track_process is not None and flac_track_process.poll() is None:
                flac_track_process.kill()
            if temp_flac_file and os.path.exists(temp_flac_file):
                try:
                    os.remove(temp_flac_file)
                except OSError:
                    pass
            raise

        else:
            return True


def remux_audio_plan_task(file_id, plan):
    """Rebuild the audio of 1 LIBRARY file to an explicit supplement plan.

    The plan has (action, source index) pairs in the format of
    plan_audio_supplements. But the plan is built by hand. Thus, a
    track can be replaced or removed. The automatic planner never does
    that. This task was made for the imperfect DTS-ES twins of the
    DTS-ES discs. [["flac", 1], ["copy", 1], ["copy", 2]] decodes the
    MA into a new 6.0 twin and removes the old 5.1 twin.

    The task copies first, like the atmos task. One staging copy comes
    in. The remux and the verification run on the local disk. The
    verified result replaces the library copy. The track rows are
    built again. The rebuilt file goes to modified/, or to transcoded/
    for a conversion (#299). The rip in untouched/ never changes.
    """

    from app.videos import acquire_lock_or_defer

    with app.app_context():
        file = db.session.get(File, int(file_id))
        if file is None:
            return True

        lock = acquire_lock_or_defer(
            file.file_identifier(),
            current_app.config["TRANSCODE_TASK_TIMEOUT"] * 1000,
            current_app.transcode_queue,
            "app.videos.remux_audio_plan_task",
            minutes=(5, 15),
            timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            args=(int(file_id), plan),
            key=int(file_id),
        )
        if not lock:
            return True

        try:
            return _remux_audio_plan_unlocked(int(file_id), plan)
        finally:
            current_app.lock_manager.unlock(lock)


def _remux_audio_plan_unlocked(file_id, plan):
    """Run the remux pipeline. The caller must hold the lock of the title."""

    from app.videos import copy_with_progress

    with app.app_context():
        job = get_current_job()
        file = db.session.get(File, int(file_id))
        basename = file.basename
        file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        if not os.path.exists(file_path):
            current_app.logger.warning(f"'{basename}' No local copy, cannot remux")
            return False

        plan = [(action, int(index)) for action, index in plan]
        staging_dir = current_app.config["STAGING_DIR"]
        # The staging names hold a digest of the library path, as in
        # remux_file (#519). 2 extras named "Trailer.mkv" can run at the
        # same time on the transcode queue.
        staged_name = f"{hashlib.sha1(file_path.encode()).hexdigest()[:8]}-{basename}"
        staging_source = os.path.join(staging_dir, f".src-{staged_name}")
        staging_output = os.path.join(staging_dir, staged_name)
        try:
            free = shutil.disk_usage(staging_dir).free
            if free < os.path.getsize(file_path) * 2.2 + 16 * 2**30:
                raise RuntimeError(f"'{basename}' not enough staging space")

            copy_with_progress(
                file_path, staging_source, job, basename, "Copying to local staging"
            )

            media_info = MediaInfo.parse(staging_source)
            container = None
            file_duration = None
            for track in media_info.tracks:
                if track.track_type == "General" and track.format:
                    container = track.format
                    # A truncated Matroska has no duration. The drift
                    # check below accepts None.
                    file_duration = (
                        int(track.duration) / 1000 if track.duration else None
                    )
            if container != "Matroska":
                raise RuntimeError(f"'{basename}' is not a Matroska file")

            audio_tracks = get_audio_tracks_from_file(staging_source)
            if any(index >= len(audio_tracks) for _, index in plan):
                raise RuntimeError(
                    f"'{basename}' plan references a missing audio track"
                )

            audio_args = build_supplement_args(plan)
            current_app.logger.info(f"'{basename}' Remux plan: {plan}")
            result = subprocess.run(
                [
                    current_app.config["FFMPEG_BIN"],
                    "-y",
                    "-i",
                    staging_source,
                    "-map",
                    "0:V",
                    "-c:v",
                    "copy",
                ]
                + audio_args
                # The attachments (subtitle fonts) stay, as in the import
                # pass. copy_image_attachments adds the cover art again.
                + ["-map", "0:s:?", "-c:s", "copy", "-map", "0:t?", "-c:t", "copy"]
                + [staging_output],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"'{basename}' ffmpeg failed: {result.stderr[-500:]}"
                )
            copy_image_attachments(staging_source, staging_output)

            # Never replace the library copy with a remux that failed. The
            # track count and the codec at each position must match the
            # plan. The duration must stay the same

            new_audio_tracks = get_audio_tracks_from_file(staging_output)
            new_subtitle_tracks = get_subtitle_tracks_from_file(staging_output)
            if len(new_audio_tracks) != len(plan):
                raise RuntimeError(
                    f"'{basename}' produced {len(new_audio_tracks)} audio "
                    f"tracks, expected {len(plan)}"
                )
            for position, (action, index) in enumerate(plan):
                produced = new_audio_tracks[position].get("format")
                expected = (
                    "FLAC" if action == "flac" else audio_tracks[index].get("format")
                )
                if produced != expected:
                    raise RuntimeError(
                        f"'{basename}' output track {position + 1} is "
                        f"{produced}, expected {expected}"
                    )
            out_info = MediaInfo.parse(staging_output)
            out_duration = None
            for track in out_info.tracks:
                if track.track_type == "General" and track.duration:
                    out_duration = int(track.duration) / 1000
            if file_duration and (
                out_duration is None or abs(out_duration - file_duration) > 5
            ):
                raise RuntimeError(
                    f"'{basename}' duration changed: {out_duration} "
                    f"vs {file_duration}"
                )

            # The S3 key comes from the row (archive_key_for), not from
            # the staging name. Thus, the digest prefix of the staging
            # name does not reach the key (#519). The first run took the
            # key from the name. It uploaded 40 GB under 'untouched/src-…'
            # (the Fellowship incident).

            os.remove(staging_source)
            final_staging = staging_output

            # A failed copy leaves a partial hidden file next to the
            # library file. Remove it, as the other write-backs do.

            hidden_library = hidden(os.path.join(os.path.dirname(file_path), basename))
            try:
                copy_with_progress(
                    final_staging,
                    hidden_library,
                    job,
                    basename,
                    "Moving into the library",
                )
                flag_archive_before_rewrite(int(file_id))
                os.replace(hidden_library, file_path)
            except BaseException:
                if os.path.exists(hidden_library):
                    os.remove(hidden_library)
                raise

            # Build the track records again because the file changed. A new
            # transaction avoids MariaDB error 1020 after the long remux.

            file = _fresh_file(file.id)
            replace_track_rows(file, new_audio_tracks, new_subtitle_tracks)
            record_filesize(file, os.path.getsize(final_staging))
            file.date_updated = datetime.now(timezone.utc)
            db.session.commit()

            # The audio layout is new. Plex reads it again now, not at its
            # own pace

            enqueue_plex_analyze(file_path)

            # The rebuilt library file goes to modified/, or to transcoded/
            # for a conversion (#299). The upload reads the staging copy.
            # A failure marks the archive stale. Then the S3 sync uploads
            # it again.

            if get_setting("ARCHIVE_ORIGINAL_MEDIA"):
                try:
                    archive_library_file(file, final_staging)
                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    db.session.rollback()
                    mark_archive_stale(
                        int(file_id), reason="re-archive after an audio remux"
                    )
                    raise
            return True

        finally:
            for stray in (staging_source, staging_output):
                try:
                    os.remove(stray)
                except OSError:
                    pass


# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, an import of this module from a process that already has an
# application does not build a second one

app = LocalProxy(get_app)
