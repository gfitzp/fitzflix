"""The first job of an import: archive the untouched file and remove the foreign tracks."""

import os
import re
import shutil
import signal
import subprocess
import time
import traceback
import uuid
from datetime import timedelta
from pymediainfo import MediaInfo
from rq import get_current_job
from sqlalchemy.exc import DBAPIError
from flask import current_app
from markupsafe import escape
from werkzeug.local import LocalProxy
from app.tasks import enqueue_task, report_progress, requeue
from app import db, get_app, makedirs_on_mounted_volume
from app.archive.uploads import discard_pending_archive, upload_untouched
from app.archive.archive_keys import pending_archive_key, pending_archive_of
from app.settings import get_setting
from app.email import notify_admin, notify_admin_text
from app.media.naming import hidden, with_extension
from app.pipeline import record_task_stage
from app.media.sdr_conversion import (
    CONVERSION_HEIGHTS,
    conversion_timeout,
    converted_file_details,
    held_decision,
    held_max_height,
    hold_import,
)
from app.media.file_ranking import find_better_files, real_rips_blocking
from app.media.tracks.languages import file_language_spelling, same_language
from app.media.tracks.scan import (
    get_audio_tracks_from_file,
    get_subtitle_tracks_from_file,
)
from app.media.tracks.remux import library_stamp, stop_process, watch_mkvmerge_progress
from app.media.tracks.audio_supplements import supplement_lossless_tracks
from app.media import videos
from app.media.importing import (
    cataloging,
    chain,
    filenames,
    follow_ups,
    library_move,
    space,
)

app = LocalProxy(get_app)


def convert_to_matroska(file_path, output_file, job, name):
    """Remux a non-Matroska file into a Matroska container.

    Return True on success. A failure means a format that mkvmerge cannot
    carry. On failure, this function removes the partial output and
    returns False. Then the caller can import the file as it is.
    """

    current_app.logger.info(f"'{name}' Converting to a Matroska container")
    process = subprocess.Popen(
        [current_app.config["MKVMERGE_BIN"], "-o", output_file, file_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    watch_mkvmerge_progress(process, job, name, "Converting to Matroska")
    try:
        videos.wait_for_subprocess(process, ok_returncodes=(0, 1))
    except subprocess.CalledProcessError:
        current_app.logger.warning(
            f"'{name}' mkvmerge could not convert this file to Matroska"
        )
        try:
            os.remove(output_file)
        except OSError:
            pass
        return False
    return True


# The import completeness gate. The first value is the number of seconds
# since the last modification. After that time, Fitzflix trusts a file
# (if the probe cannot read its container). The second value is the
# number of 1-minute checks a file gets before Fitzflix imports it in all
# cases.

COMPLETENESS_QUIET_SECONDS = 120


MAX_COMPLETENESS_RETRIES = 30


# The number of seconds between the 2 size readings of an incoming file.
# A file that grows in this time is still being copied. The test suite
# sets a shorter time.

GROWTH_CHECK_SECONDS = 5


# Containers that declare their own length. Thus, MediaInfo can prove
# that a stalled partial copy is truncated, not complete.

SELF_SIZING_FORMATS = {"Matroska", "MPEG-4"}


def probe_file_completeness(file_path):
    """Ask the container if the file is structurally complete.

    A Matroska file declares its segment size. An MP4 file indexes itself
    in a trailing moov atom. Thus, MediaInfo reports truncation for a
    partial copy of either. The time that the copy has stalled is not
    important. Return True if such a container looks complete. Return
    False if it reports truncation. Return None for a file that the probe
    cannot read (an unidentifiable file, or a format with no declared
    length).
    """

    try:
        media_info = MediaInfo.parse(file_path)
    except Exception:
        return None

    general = next(
        (track for track in media_info.tracks if track.track_type == "General"),
        None,
    )
    if general is None or general.format not in SELF_SIZING_FORMATS:
        return None
    if str(general.to_data().get("istruncated", "")).lower() == "yes":
        return False
    return True


# Tasks


class _NotedStrays(list):
    """The work files of a run. Each new one goes into the note of the run."""

    def __init__(self, run):
        super().__init__()
        self.run = run

    def append(self, path):
        """Add a work file, and note the run again."""

        super().append(path)
        self.run.note()


class _LocalizationRun:
    """Hold the state that the phases of 1 localization run share.

    localization_task owns the one exception handler of the run. That
    handler needs the title lock and the work files of the run. Thus,
    those 2 values live here. A phase that changes them says so."""

    def __init__(self, source_path, counters):
        self.source_path = source_path
        self.basename = os.path.basename(source_path)
        self.counters = counters
        self.job = None
        self.lock = None
        self.strays = _NotedStrays(self)
        self.pending = None
        # An in-place import rewrites the source itself (#424). A stop
        # signal during that rewrite must not leave it for a re-import.
        self.in_place = False

    def note(self):
        """Keep the lock, the pending upload and the strays in Redis (#498).

        A worker that dies takes them with it. The worker that reclaims
        the dead job reads them here. This is best effort."""

        from app.media.worker_recovery import note_job_state

        job_id = getattr(self.job, "id", None)
        if job_id is None:
            return
        try:
            note_job_state(
                job_id,
                current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                lock=self.lock,
                pending=self.pending,
                strays=list(self.strays),
            )
        except Exception:
            current_app.logger.warning(traceback.format_exc())

    def retry(self, delay, reason, level="warning", **changes):
        """Book the task again after delay. Every retry carries all 4 counters.

        Each retry changes only its own counter. Return "retry". A
        requeue that fails raises HandoffFailed (#559). The handler of
        localization_task then keeps the source."""

        handoff(
            requeue,
            "app.media.importing.localization.localization_task",
            key=self.source_path,
            delay=delay,
            reason=reason,
            counters={**self.counters, **changes},
            kwargs={"file_path": self.source_path},
            description=f"'{self.basename}'",
            level=level,
        )
        return "retry"

    def release_lock(self):
        """Release the title lock of the run and log it."""

        current_app.lock_manager.unlock(self.lock)
        current_app.logger.info(f"Removed lock {self.lock}")


def localization_task(
    file_path,
    transient_retries=0,
    completeness_retries=0,
    space_waits=0,
    tmdb_retries=0,
):
    """Archive an untouched file and remove the unnecessary language tracks.

    - This task uploads the untouched file to AWS S3 storage as a backup.
    - It localizes the file. It keeps all native-language audio and subtitle
      tracks. It also keeps the first audio track if that track is not in
      the native language.
    - It passes the localized file to a separate process. That process adds
      the file to the database.
    - It waits while the library volume has too little free space.
      space_waits counts the waits.

    The phases run in this order: _admission_gate, _identify,
    _take_title_lock, _quality_and_space_gate, _stage_source,
    _archive_original, _localize, and _hand_off. A phase that stops the
    run returns a verdict. A failure in any phase before the hand-off
    goes to the one handler here, which calls abandon_import.
    """

    with app.app_context():
        run = _LocalizationRun(
            file_path,
            {
                "transient_retries": transient_retries,
                "completeness_retries": completeness_retries,
                "space_waits": space_waits,
                "tmdb_retries": tmdb_retries,
            },
        )

        try:
            run.job = get_current_job()
            file_details = None

            verdict = _admission_gate(file_path, run)
            if verdict != "ok":
                return verdict == "retry"

            identified = _identify(file_path, run)
            if isinstance(identified, str):
                return identified == "retry"
            file_details, conversion = identified

            run.lock = _take_title_lock(file_details, run)
            if not run.lock:
                return False

            verdict = _quality_and_space_gate(file_path, file_details, conversion, run)
            if verdict != "ok":
                return verdict == "wait"

            # Save the untouched filename. Fitzflix can need it to recreate
            # the file.

            file_details["untouched_basename"] = run.basename

            # Radarr and Sonarr can put a second download at the path of
            # a source in the library during the chain (#742). The stamp
            # lets the later stages see that.

            if follow_ups._in_library(file_path):
                file_details["source_stamp"] = library_stamp(file_path)

            staging = _stage_source(file_path, run)
            if staging is None:
                return True
            work_path, staged = staging

            _archive_original(work_path, file_details)
            run.pending = pending_archive_of(file_details)
            run.note()

            # The untouched upload does not change the source. Only the
            # localization of an in-place import rewrites it (#497).
            run.in_place = not staged
            work_path, hidden_output_file = _localize(
                file_path, work_path, file_details, staged, run.job, run.strays
            )

            # The localization of an unstaged source can write the source
            # itself. Then the stamp is the stamp after that work.

            if not staged and file_details.get("source_stamp"):
                file_details["source_stamp"] = library_stamp(file_path)

        except HandoffFailed:
            # The run could not book its own retry or take its title lock
            # (#559). Only Redis failed. The retries all come before the
            # localization, so nothing has changed the source. It stays
            # for the hourly sweep.

            current_app.logger.error(
                f"'{run.basename}' A retry could not be queued. The source "
                f"stays for the next sweep: " + traceback.format_exc()
            )
            abandon_import(
                run.source_path,
                run.lock,
                strays=run.strays,
                keep_source=True,
                pending_archive=pending_archive_of(file_details),
            )
            return False

        except BaseException as error:
            # Remove the staged copies. Fitzflix rejects the original source.
            # Nothing has changed a staged source. A file too large for the
            # staging volume is the exception (#424). It is processed in
            # place, and the track statistics, the FLAC twins, or the MP4
            # metadata removal can rewrite it before a failure. Such a
            # rewrite removes no stream of the file. The untouched archive
            # goes up first. An upload to the normal key stays in S3, and
            # the next try can replace it. A run-scoped upload goes with
            # the failed run, as for a staged source. A stop signal keeps
            # the source (#497). An outage of the database keeps it too,
            # and the job fails (#681).

            abandon_failed_stage(
                error,
                run.source_path,
                run.lock,
                may_keep=not run.in_place,
                strays=run.strays,
                pending_archive=pending_archive_of(file_details),
            )

        else:
            return _hand_off(
                run, work_path, file_details, hidden_output_file, staged, conversion
            )

        return True


def _admission_gate(file_path, run):
    """Decide if an incoming file can start its import. Return "ok", "retry", or "stop".

    "retry" means that the task is booked again (a dead volume, an empty
    file, a file that grows, or a file that is not complete yet). "stop" means that
    the task ends with nothing to do, or after a reject."""

    basename = run.basename
    completeness_retries = run.counters["completeness_retries"]

    # Do not start if a necessary volume is dead. A mount that fails
    # during the task strands partial files. Thus, defer to a retry.

    dead = videos._dead_volumes(
        [
            os.path.dirname(file_path),
            current_app.config["MOVIE_LIBRARY"],
            current_app.config["TV_LIBRARY"],
            current_app.config["MEDIA_LOCATION"],
            current_app.config["STAGING_DIR"],
        ]
    )
    if dead:
        return run.retry(
            timedelta(minutes=5), f"Volumes unavailable ({', '.join(dead)})"
        )

    # If the incoming file does not exist, there is nothing to do.

    if not os.path.exists(file_path):
        return "stop"

    # A duplicate job or a late retry can reach a file that an earlier
    # job already imported in place (#287).

    if chain.already_cataloged(file_path):
        current_app.logger.info(
            f"'{basename}' Is already in the library; nothing to import"
        )
        return "stop"

    # The work files add up to 22 bytes to the name ("." and
    # ".localized", ".remux.mkv", and others). A name system allows 255
    # bytes. A longer name made mkvmerge fail with no clear reason.

    if len(basename.encode("utf-8")) > filenames.MAX_NAME_BYTES:
        current_app.logger.error(
            f"'{basename}' has a name of more than {filenames.MAX_NAME_BYTES} bytes. "
            f"Rename it shorter"
        )
        filenames.move_to_rejects(file_path, "name too long")
        return "stop"

    # If the file name contains "temp-1234.", ignore the file.

    if re.search(r"\-temp\-\d+\.", basename):
        return "stop"

    # Do not process a file that a copy is still writing. If the file
    # grows, check again in 1 minute.

    size_before = os.path.getsize(file_path)

    # Invariant: an empty file is never imported (#777). The Finder
    # copies files one at a time, and it makes an empty file for each
    # one that waits. Such a file passed each check below: its size
    # does not grow, MediaInfo cannot read it, and nothing writes to
    # it. It waits as a file that grows does. No check is counted, so
    # the completeness budget cannot import it.

    if size_before == 0:
        return run.retry(
            timedelta(minutes=1), "is empty, its copy did not start", level="info"
        )
    time.sleep(GROWTH_CHECK_SECONDS)
    if os.path.getsize(file_path) != size_before:
        return run.retry(timedelta(minutes=1), "is still being copied", level="info")

    # A stalled network copy can fool the size check above. Its size
    # stays constant while the file is still incomplete. Let the
    # container prove completeness where it can. A partial Matroska or
    # MP4 file reports truncation. The time that the copy has stalled is
    # not important. Give a format that the probe cannot read a
    # modification-time quiet period instead. Fitzflix imports a file
    # that never proves itself within the budget in all cases, because
    # Fitzflix imports a corrupt-but-complete file as it is by design.

    verdict = probe_file_completeness(file_path)
    if verdict is False or (
        verdict is None
        and time.time() - os.path.getmtime(file_path) < COMPLETENESS_QUIET_SECONDS
    ):
        if completeness_retries >= MAX_COMPLETENESS_RETRIES:
            current_app.logger.warning(
                f"'{basename}' Could not be confirmed complete after "
                f"{MAX_COMPLETENESS_RETRIES} checks, importing anyway"
            )
        else:
            reason = (
                "is still incomplete"
                if verdict is False
                else "was modified too recently"
            )
            return run.retry(
                timedelta(minutes=1),
                f"{reason} (check {completeness_retries + 1} of "
                f"{MAX_COMPLETENESS_RETRIES})",
                level="info",
                completeness_retries=completeness_retries + 1,
            )

    return "ok"


def _identify(file_path, run):
    """Parse the name of an import and plan its conversion.

    Return (file_details, conversion), where conversion is None for a
    file that needs no conversion. Return "retry" after a TMDB outage,
    or "stop" after a reject."""

    basename = run.basename
    tmdb_retries = run.counters["tmdb_retries"]

    file_details = filenames.evaluate_filename(file_path)
    current_app.logger.info(f"'{basename}' File details: {file_details}")
    if not file_details:
        reason = getattr(file_details, "reason", None)

        # The TMDB tries have their own count. The transient count limits
        # the retries of an I/O error in the staging copy.

        if (
            reason == filenames.TMDB_UNREACHABLE
            and tmdb_retries < filenames.TMDB_RETRIES
        ):
            return run.retry(
                timedelta(minutes=30),
                "TMDB is unreachable",
                tmdb_retries=tmdb_retries + 1,
            )
        current_app.logger.error(
            f"'{basename}' rejected: {reason}!"
            if reason
            else f"'{basename}' doesn't match expected naming formats!"
        )
        filenames.move_to_rejects(file_path, reason or "incorrect filename")
        return "stop"

    # An HDR or BT.2020 import becomes an SDR conversion (#292). The
    # HDR_IMPORT_MODE setting gives its maximum height, or no height for
    # the mode that keeps HDR (#657). The plan renames the import to the
    # quality of the conversion now. Thus, the quality checks compare
    # the conversion, not the original.

    # The decision on a held file does not depend on a setting that
    # changed while the file waited (#677). The file converts to the
    # height that its hold stored, and the mode is not read for it.

    conversion = None
    held = held_decision(file_path)
    max_height = held_max_height(held) if held is not None else None
    if max_height is None:
        max_height = CONVERSION_HEIGHTS.get(get_setting("HDR_IMPORT_MODE"))
        if held is not None:
            # A hold from before #677 stored no height. An admin chose
            # to import a conversion. Thus, the mode "keep" gives 1080.
            max_height = max_height or 1080
    if max_height:
        conversion = library_move._plan_conversion_or_reject(
            file_path, file_details, max_height
        )
        if conversion is False:
            return "stop"
        if conversion:
            file_details = converted_file_details(file_details, conversion)
            current_app.logger.info(
                f"'{basename}' {conversion['source_format']} "
                f"{conversion['source_quality']}. Importing as a "
                f"{conversion['target_quality']} SDR conversion"
            )
    return file_details, conversion


def _take_title_lock(file_details, run):
    """Take the title lock of an import. Return the lock, or None after a deferral.

    Fitzflix must not process a different version of this video at the
    same time. Thus, the lock key comes from specific movie or TV show
    fields. A different file with the same key waits until the
    processing of the first file is complete. Without the lock, the task
    returns to the localization queue. The queue tries it again when the
    lock becomes available."""

    file_identifier = chain.lock_key_for_details(file_details)
    current_app.logger.debug(f"'{run.basename}' Lock identifier: {file_identifier}")

    # The note of the lock follows the take in the same call (#498). A
    # worker that dies after this still leaves its lock token behind.

    # A Redis fault of the lock or of the deferral raises HandoffFailed
    # (#559). The source is good, so the handler keeps it.

    lock = handoff(
        videos.acquire_lock_or_defer,
        file_identifier,
        current_app.config["LOCALIZATION_TASK_TIMEOUT"] * 1000,
        current_app.import_queue,
        "app.media.importing.localization.localization_task",
        minutes=(45, 75),
        timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
        description=f"'{run.basename}'",
        kwargs={"file_path": run.source_path, **run.counters},
        key=run.source_path,
    )
    if lock:
        run.lock = lock
        run.note()
    return lock


def _quality_and_space_gate(file_path, file_details, conversion, run):
    """Decide if a locked import goes on. Return "ok", "hold", "reject", or "wait".

    "hold" waits for an admin, "reject" sends an inferior file to
    rejects, and "wait" books the task again for library space. Each of
    the 3 releases the title lock. "wait" also clears run.lock."""

    basename = run.basename
    space_waits = run.counters["space_waits"]

    # A conversion waits for an admin when the library has a real rip of
    # the title at its quality or better. "Import" sends the file back
    # here with replace_existing.

    if conversion:
        if held_decision(file_path):
            file_details["replace_existing"] = True
        else:
            blockers = real_rips_blocking(file_details)
            if blockers:
                held = hold_import(file_path, file_details, conversion, blockers)
                run.release_lock()
                return "hold" if held is not None else "reject"

    # A record has this path in an other letter case, and its file is
    # still on the disk (#749). The 2 files can be different works, for
    # example 2 extras of a film. Only 1 can have a record. A person
    # decides. The record, its file and its archive stay as they are.

    other_spelling = cataloging._file_at_other_spelling(file_path, file_details)
    if other_spelling:
        current_app.logger.warning(
            f"'{basename}' The library has '{other_spelling}', whose name "
            f"differs only by letter case or accents. Fitzflix cannot hold "
            f"both. The new file goes to the rejects"
        )
        filenames.move_to_rejects(file_path, "other spelling")
        run.release_lock()
        notify_admin_text(
            "Rejected a file whose name differs only by letter case or accents",
            f"'{basename}' went to the rejects folder 'other spelling'. The "
            f"library has '{other_spelling}', and Fitzflix can hold only 1 of "
            f"the 2 names. Give 1 of the files an other name, or delete it.",
            f"<p>'{escape(basename)}' went to the rejects folder 'other "
            f"spelling'. The library has '{escape(other_spelling)}', and "
            f"Fitzflix can hold only 1 of the 2 names. Give 1 of the files an "
            f"other name, or delete it.</p>",
        )
        return "reject"

    # Look for better-quality versions of this file that already exist.

    better_versions = find_better_files(file_details)
    if better_versions:
        current_app.logger.info(
            f"Better versions of '{basename}' exist; skipping import"
        )
        for better in better_versions:
            current_app.logger.debug(vars(better))

        filenames.move_to_rejects(file_path, "inferior quality")
        run.release_lock()
        notify_admin(
            "Received an inferior-quality file",
            "inferior_warning",
            basename=basename,
            better_versions=better_versions,
            rejects_directory=current_app.config["REJECTS_DIR"],
        )
        return "reject"

    # Wait for free space on the library volume before the heavy work.
    # Without the space, the final copy would fail and discard the work.
    # The wait releases the title lock (#262).

    output_directory = os.path.join(
        current_app.config["LIBRARY_DIR"], file_details.get("dirname")
    )
    needed = int(os.path.getsize(file_path) * space.SPACE_FACTOR) + space.SPACE_RESERVE
    shortfall = space.library_space_shortfall(output_directory, needed)
    if shortfall:
        space._log_space_wait(
            basename, output_directory, needed, shortfall, space_waits
        )
        run.release_lock()
        run.lock = None
        run.retry(
            space.SPACE_WAIT, "waits for space", level=None, space_waits=space_waits + 1
        )
        return "wait"
    if space_waits:
        current_app.logger.info(
            f"'{basename}' Continuing the import after {space_waits} "
            f"wait(s) for space"
        )
    return "ok"


def _stage_source(file_path, run):
    """Copy the source to local staging. Return (work_path, staged), or None.

    The archive upload and the localization tools then do their heavy
    I/O on the local disk. A network failure costs a retry, not a
    stranded partial file. None means that the task is booked again.
    The staged copy goes in run.strays.

    The space check counts what the other imports on staging still need
    (#294). Without enough space, the import waits and releases the
    title lock. Only a file that cannot fit on the staging volume at all
    is processed on its source volume. Then staged is False."""

    basename = run.basename
    space_waits = run.counters["space_waits"]
    transient_retries = run.counters["transient_retries"]
    staging_dir = current_app.config["STAGING_DIR"]
    staging_needed = int(os.path.getsize(file_path) * space.STAGING_FACTOR)
    staging = space.reserve_staging(basename, staging_needed, [basename], staging_dir)
    if staging == "lock":
        # The space was not checked. Thus, this is no space wait, and the
        # space-wait counter stays as it is (#478).
        current_app.logger.info(
            f"'{basename}' The staging lock is busy. Trying again in "
            f"{int(space.STAGING_LOCK_RETRY.total_seconds() // 60)} minute"
        )
        run.release_lock()
        run.lock = None
        run.retry(space.STAGING_LOCK_RETRY, "waits for the staging lock", level=None)
        return None
    if staging == "wait":
        current_app.logger.info(
            f"'{basename}' Needs {space._gigabytes(staging_needed)} on staging. "
            f"The other imports still need "
            f"{space._gigabytes(space.staging_outstanding(staging_dir, exclude=basename))}. "
            f"Trying again in {int(space.SPACE_WAIT.total_seconds() // 60)} minutes"
        )
        run.release_lock()
        run.lock = None
        run.retry(
            space.SPACE_WAIT,
            "waits for staging space",
            level=None,
            space_waits=space_waits + 1,
        )
        return None

    if staging != "ok":
        current_app.logger.warning(
            f"'{basename}' Does not fit on the staging volume, "
            f"processing on the source volume instead"
        )
        return file_path, False

    staged_path = os.path.join(staging_dir, basename)
    run.strays.append(staged_path)
    record_task_stage("Copying to staging", "started")
    try:
        videos.copy_with_progress(
            file_path, staged_path, run.job, basename, "Copying to local staging"
        )
    except OSError as e:
        record_task_stage("Copying to staging", "failed")
        if (
            e.errno not in videos.TRANSIENT_COPY_ERRNOS
            or transient_retries >= videos.MAX_TRANSIENT_RETRIES
        ):
            raise

        # An unstable mount revoked the file handles of the copy. The
        # source is not damaged. Thus, clean up and retry after the mount
        # settles. Do not reject a good file.

        try:
            os.remove(staged_path)
        except OSError:
            pass
        run.release_lock()
        run.retry(
            timedelta(minutes=5),
            f"Staging copy failed with a transient I/O error ({e}) "
            f"(attempt {transient_retries + 1} of {videos.MAX_TRANSIENT_RETRIES})",
            transient_retries=transient_retries + 1,
        )
        return None

    record_task_stage("Copying to staging", "done")
    return staged_path, True


def _archive_original(work_path, file_details):
    """Upload the untouched file to AWS S3 storage as a backup, if the setting is on.

    A surviving record can claim the untouched key for different bytes.
    Then the upload goes to a run-scoped key below pending-untouched/
    (#430). The record of the import claims that key. After the commit,
    an archive move takes it to its final key. A failed import deletes
    it, unless the localized file replaced the source (#727). Thus, a
    failure never replaces the archive of a good rip."""

    if get_setting("ARCHIVE_ORIGINAL_MEDIA"):
        run_id = uuid.uuid4().hex[:12]
        (
            file_details["aws_key"],
            file_details["aws_date_uploaded"],
            file_details["aws_filesize_bytes"],
        ) = upload_untouched(work_path, lambda key: pending_archive_key(key, run_id))


def _localize(source_path, work_path, file_details, staged, job, strays):
    """Write the localized output of an import. Return (work_path, hidden_output_file).

    A non-Matroska file first becomes Matroska where mkvmerge can carry
    it. Then work_path is the converted file. A Matroska file loses its
    non-native tracks. Any other file is copied as it is. Each work file
    that this step makes goes in strays."""

    basename = os.path.basename(source_path)
    staging_dir = current_app.config["STAGING_DIR"]

    current_app.logger.info(f"'{basename}' Starting localization process")

    output_directory = os.path.join(
        current_app.config["LIBRARY_DIR"], file_details.get("dirname")
    )

    # Parse the incoming file and get its details with MediaInfo.

    current_app.logger.info(f"'{basename}' Parsing with MediaInfo")
    media_info = MediaInfo.parse(work_path)
    current_app.logger.debug(f"'{basename}' -> {media_info.to_json()}")

    for track in media_info.tracks:
        if track.track_type == "General" and track.format:
            current_app.logger.info(f"'{basename}' File container {track.format}")
            file_details["container"] = track.format

    # Fitzflix first remuxes a non-Matroska file into a Matroska container.
    # Thus, every importable format gets the same localization. Fitzflix
    # imports a format that mkvmerge cannot carry as it is.

    if file_details.get("container") != "Matroska":
        scratch_dir = staging_dir if staged else output_directory
        makedirs_on_mounted_volume(scratch_dir)
        converted_file = hidden(os.path.join(scratch_dir, basename), ".convert.mkv")
        strays.append(converted_file)

        if convert_to_matroska(work_path, converted_file, job, basename):
            # Use the converted file. Rename the final output to agree with
            # its new container.

            if work_path != source_path:
                try:
                    os.remove(work_path)
                except OSError:
                    pass
            work_path = converted_file
            file_details["container"] = "Matroska"
            file_details["basename"] = with_extension(file_details["basename"], "mkv")
            file_details["extension"] = "mkv"
            file_details["file_path"] = os.path.join(
                file_details["dirname"], file_details["basename"]
            )

        else:
            current_app.logger.warning(
                f"'{basename}' Can't be converted to Matroska, importing as-is"
            )

    # If staging is on, Fitzflix writes the localized output next to the
    # staged copy. Thus, only the complete file goes to the library.

    # On staging, the name comes from the source. The library name of an
    # extra is only the extra, for example "Trailer.mkv". The extras of 2
    # films would then write the same staging file.

    # The partial output of a failed step is a stray in both places
    # (#389). Without staging, it is a hidden file in the library folder.

    if staged:
        hidden_output_file = space.staged_output_path(
            staging_dir, basename, file_details.get("basename")
        )
    else:
        hidden_output_file = hidden(
            os.path.join(output_directory, file_details.get("basename"))
        )
    strays.append(hidden_output_file)

    # Export a localized version of the incoming file.

    if file_details.get("container") == "Matroska":
        _localize_matroska(
            work_path, hidden_output_file, output_directory, basename, job
        )
    else:
        _copy_as_is(
            work_path,
            hidden_output_file,
            output_directory,
            basename,
            file_details.get("container"),
            job,
        )
    return work_path, hidden_output_file


def _localize_matroska(work_path, hidden_output_file, output_directory, basename, job):
    """Write the localized Matroska output of work_path to hidden_output_file."""

    current_app.logger.info(f"'{basename}' Localizing as a Matroska file")

    # Give each lossless track that is not FLAC or PCM a FLAC twin
    # immediately before it. An Apple TV client can play FLAC natively.
    # Always keep the original for direct play and for future
    # passthrough. A file whose twins already exist (a MakeMKV "FLAC Plus
    # Original Audio" rip, or a re-download of a supplemented upload)
    # passes through as it is. CREATE_FLAC_TWINS turns this step off for
    # passthrough players.

    if not get_setting("CREATE_FLAC_TWINS"):
        current_app.logger.info(f"'{basename}' CREATE_FLAC_TWINS is off. No FLAC twins")
    else:
        supplement_lossless_tracks(work_path)

    # Sometimes the input mkv file has no track details, for example the
    # number of subtitle elements in a subtitle track. Fitzflix needs
    # those details to tell if there is possibly a forced subtitle track.
    # This command adds the details to the file if they are missing.

    current_app.logger.info(f"'{basename}' Adding track statistics tags")

    statistics_tags_process = subprocess.Popen(
        [
            current_app.config["MKVPROPEDIT_BIN"],
            "--add-track-statistics-tags",
            work_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    watch_mkvmerge_progress(
        statistics_tags_process, job, basename, "Adding track statistics tags"
    )
    videos.wait_for_subprocess(statistics_tags_process, ok_returncodes=(0, 1))

    # Parse the file again, because it now has the track statistics tags.

    current_app.logger.info(f"'{basename}' Parsing added statistics with MediaInfo")
    media_info = MediaInfo.parse(work_path)
    current_app.logger.debug(f"'{basename}' -> {media_info.to_json()}")
    audio_tracks = get_audio_tracks_from_file(work_path)
    subtitle_tracks = get_subtitle_tracks_from_file(work_path)

    # ISO 639-2 gives 20 languages 2 codes. MediaInfo reports the
    # terminological code for each file with a LanguageIETF element,
    # whatever wrote it ("deu" for a track written as "ger"). Thus, the
    # comparisons use same_language, and the mkvmerge filters use the
    # spelling of the file (#423).

    native_language = file_language_spelling(
        current_app.config["NATIVE_LANGUAGE"], audio_tracks
    )
    native_subtitle_language = file_language_spelling(
        current_app.config["NATIVE_LANGUAGE"], subtitle_tracks
    )

    # Find the audio tracks to export.

    # If there are no audio tracks, mkvmerge could use the --no-audio
    # flag. But the default of the first audio track is sufficient,
    # because no track will exist.

    if len(audio_tracks) == 0:
        first_audio_track_language = "1"

    elif audio_tracks[0].get("language"):
        first_audio_track_language = audio_tracks[0].get("language")

    else:
        first_audio_track_language = 1

    # If the first audio track is in the native language, remove all other
    # languages.

    if len(audio_tracks) >= 1 and same_language(
        first_audio_track_language, native_language
    ):
        current_app.logger.info(
            f"'{basename}' First audio track matches native language "
            f"'{native_language}'"
        )
        output_audio_langs = native_language

    # The first audio track can be in a language that is not the native
    # language, while the native language is present. Then export the
    # tracks in the first language and all native-language audio. The
    # file is probably a dub, or it has native-language commentary tracks.

    elif any(
        same_language(track["language"], native_language) for track in audio_tracks
    ):
        current_app.logger.info(
            f"'{basename}' First audio track is foreign, "
            f"but '{native_language}' audio is present"
        )
        output_audio_langs = f"{first_audio_track_language},{native_language}"

    # If there is no native-language track, export only the tracks in the
    # first language. The file is probably a subtitled movie with no
    # commentary track.

    else:
        current_app.logger.info(f"'{basename}' No '{native_language}' audio track")
        output_audio_langs = first_audio_track_language

    # Find the tracks to export and make the output file.

    makedirs_on_mounted_volume(output_directory)

    first_audio_foreign = len(audio_tracks) >= 1 and not same_language(
        first_audio_track_language, native_language
    )
    native_subtitles = any(
        same_language(track["language"], native_language) for track in subtitle_tracks
    )
    if first_audio_foreign and native_subtitles:
        current_app.logger.info(
            f"'{basename}' Non-native audio, "
            f"but '{native_language}' subtitles are present"
        )
    elif native_subtitles:
        current_app.logger.info(f"'{basename}' '{native_language}' audio and subtitles")
    elif subtitle_tracks:
        current_app.logger.info(f"'{basename}' No '{native_language}' subtitles")
    else:
        current_app.logger.info(f"'{basename}' No subtitles whatsoever")

    mkvmerge_process = subprocess.Popen(
        build_localize_command(
            current_app.config["MKVMERGE_BIN"],
            work_path,
            hidden_output_file,
            output_audio_langs,
            native_subtitle_language,
            subtitle_tracks,
            first_audio_foreign,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    watch_mkvmerge_progress(mkvmerge_process, job, basename, "Localizing")
    videos.wait_for_subprocess(mkvmerge_process, ok_returncodes=(0, 1))


def build_localize_command(
    mkvmerge,
    source,
    output,
    audio_langs,
    native_language,
    subtitle_tracks,
    first_audio_foreign,
):
    """Return the mkvmerge command that localizes source into output.

    audio_langs is the value of -a. With native-language subtitles, the
    command keeps only those subtitles. Then, if the first audio track is
    foreign, the first native-language subtitle track becomes the default
    and the others do not. If the audio is native, no native-language
    subtitle track is a default. Without native-language subtitles, the
    command keeps no subtitles. The output has no title and no track
    names. This function reads nothing and runs nothing."""

    native_tracks = [
        track
        for track in subtitle_tracks
        if same_language(track["language"], native_language)
    ]
    if native_tracks:
        flags = []
        for index, track in enumerate(native_tracks):
            on = first_audio_foreign and index == 0
            flags.extend(
                ["--default-track-flag", f"{track['streamorder']}:{1 if on else 0}"]
            )
        spellings = list(dict.fromkeys(track["language"] for track in native_tracks))
        subtitles = ["-s", ",".join(spellings)] + flags
    else:
        subtitles = ["--no-subtitles"]
    return (
        [mkvmerge, "-o", output, "-a", audio_langs]
        + subtitles
        + ["--title", "", "--track-name", "-1:", source]
    )


def _copy_as_is(
    work_path, hidden_output_file, output_directory, basename, container, job
):
    """Copy a file that is not Matroska to its hidden output.

    An MPEG-4 file first loses its metadata through AtomicParsley."""

    if container == "MPEG-4":
        current_app.logger.info(f"'{basename}' Removing MPEG-4 metadata")
        atomicparsley_process = subprocess.Popen(
            [
                current_app.config["ATOMICPARSLEY_BIN"],
                work_path,
                "--metaEnema",
                "--overWrite",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
        try:
            for line in atomicparsley_process.stdout:
                line = line.replace("\n", "")
                current_app.logger.info(f"'{os.path.basename(work_path)}' {line}")
                report_progress(
                    job, os.path.basename(work_path), "Removing MPEG-4 metadata"
                )
        except BaseException:
            # AtomicParsley rewrites the work file in place. It must stop
            # before the cleanup (#499).
            stop_process(atomicparsley_process)
            raise

        videos.wait_for_subprocess(atomicparsley_process)

        current_app.logger.info(f"'{basename}' Removed MPEG-4 metadata")

    else:
        current_app.logger.info(f"'{basename}' Not Matroska or MPEG-4, importing as-is")

    current_app.logger.info(f"'{basename}' Copying to '{hidden_output_file}'")
    makedirs_on_mounted_volume(output_directory)
    report_progress(job, basename, "Copying to destination")
    shutil.copy(work_path, hidden_output_file)


def _hand_off(run, work_path, file_details, hidden_output_file, staged, conversion):
    """Give the localized output to the next stage. Return True, or False on a failure.

    The working copy (the staged source or the conversion temp file) is
    no longer necessary. The file-operation queue carries the localized
    output to the library. Then it gives the quick database work to the
    sql queue. A conversion first goes to the transcode queue."""

    source_path, basename, lock = run.source_path, run.basename, run.lock

    if work_path != source_path:
        try:
            os.remove(work_path)
        except OSError:
            pass

    # The localized file stays on staging until its move, and a
    # conversion writes more next to it. Shrink the reservation to what
    # remains (#294).

    # A failure here (Redis away at the enqueue) must not keep the title
    # lock for its full TTL (#340). The source stays in the import
    # directory for the hourly sweep.

    try:
        if staged:
            output_size = os.path.getsize(hidden_output_file)
            space.set_staging_reservation(
                basename,
                output_size * (space.CONVERSION_STAGING_FACTOR if conversion else 1),
                [basename],
            )

        # A conversion first goes to the transcode queue. That task gives
        # the file to move_localized_file after the encode. Its timeout
        # comes from the length of the film (#674).

        if conversion:
            enqueue_task(
                "app.media.sdr_conversion.convert_localized_file",
                source_path,
                file_details,
                lock,
                hidden_output_file,
                conversion,
                description=f"'{file_details.get('basename')}'",
                timeout=conversion_timeout(conversion),
            )
        else:
            enqueue_task(
                "app.media.importing.library_move.move_localized_file",
                source_path,
                file_details,
                lock,
                hidden_output_file,
                description=f"'{basename}'",
            )
    except Exception:
        abandon_handoff(
            source_path,
            lock,
            hidden_output_file,
            pending_archive=pending_archive_of(file_details),
        )
        return False
    return True


# A supervisor stop, a restart, or a shutdown sends these signals. The
# file is good. Thus, a stage that they stop keeps its source (#497).

INTERRUPT_SIGNALS = frozenset({signal.SIGTERM, signal.SIGINT, signal.SIGHUP})


def interrupted(error):
    """Return True if a stop signal ended the task or its child process (#497).

    A child that a signal stops has a negative return code. A second
    SIGTERM to an rq worker raises SystemExit inside the job."""

    if isinstance(error, (SystemExit, KeyboardInterrupt)):
        return True
    code = getattr(error, "returncode", None)
    return (
        isinstance(error, subprocess.CalledProcessError)
        and isinstance(code, int)
        and -code in INTERRUPT_SIGNALS
    )


# The pymysql error codes of a lost or refused connection to MariaDB
# (#681): cannot connect through the socket or to the host, the server
# went away, and a connection lost during a query.

DATABASE_OUTAGE_CODES = frozenset({2002, 2003, 2006, 2013})


def database_outage(error):
    """Return True if a lost or refused connection to MariaDB ended the stage (#681).

    The file is good. The database did not answer. A constraint error, a
    deadlock (1213), a lock wait timeout (1205), or a changed record
    (1020) is not an outage."""

    if not isinstance(error, DBAPIError):
        return False
    if error.connection_invalidated:
        return True
    args = getattr(error.orig, "args", None) or ()
    return bool(args) and args[0] in DATABASE_OUTAGE_CODES


def _in_import_directory(path):
    """Return True if path is a file of the import directory."""

    import_dir = os.path.abspath(current_app.config["IMPORT_DIR"])
    return os.path.dirname(os.path.abspath(path)) == import_dir


def abandon_failed_stage(
    error, source_path, lock, may_keep=True, later_stage=False, **kwargs
):
    """Clean up a stage after error. Re-raise a stop signal, and an outage that keeps the source.

    A stop signal keeps the source (#497). The hourly sweep finds a
    source of the import directory. The hourly retry of the in-place
    imports finds a failed localization of a download in the library.
    The weekly sync finds each other download there: a file with no
    record, and a file with an other size than its record (#750). A signal that
    stopped only the child tool is not raised again. Any other error
    rejects the source. With may_keep False, a stop signal
    rejects the source too, and an outage is a plain error that does
    not raise again. A caller that passes keep_source=True keeps the
    source for each error (#688). The cataloging does that for a file
    that a record points to.

    An outage of the database keeps the source only when a sweep finds
    it again (#681). The hourly sweep of the import directory queues
    each file there. The hourly retry of the in-place imports reads
    only the failed localization jobs. Thus, the localization keeps
    each source for an outage, and its job fails. A later stage
    (later_stage) keeps only a source of the import directory. It
    rejects a download in the library, as each other error does. A
    person then sees it on the Rejects page. The cleanup needs no
    database. The session rolls back last."""

    name = os.path.basename(source_path)
    outage = (
        may_keep
        and database_outage(error)
        and (not later_stage or _in_import_directory(source_path))
    )
    if interrupted(error) and (may_keep or kwargs.get("keep_source")):
        current_app.logger.warning(
            f"'{name}' A stop signal interrupted the import ({error!r}). The "
            f"source stays"
        )
        kwargs["keep_source"] = True
    elif outage:
        current_app.logger.warning(
            f"'{name}' The database did not answer ({error.orig!r}). The source "
            f"stays for the next sweep"
        )
        kwargs["keep_source"] = True
    else:
        current_app.logger.error(traceback.format_exc())
    abandon_import(source_path, lock, **kwargs)
    if outage:
        try:
            db.session.rollback()
        except Exception:
            current_app.logger.warning(traceback.format_exc())
    if outage or not isinstance(error, Exception):
        raise error


def abandon_import(
    source_path,
    lock,
    strays=(),
    reservation=None,
    reason="exception",
    keep_source=False,
    pending_archive=None,
):
    """Clean up an import stage that failed. Each step is best effort.

    Release the title lock, remove the stray work files, release the
    staging reservation, and move the source to rejects with reason.
    With keep_source, the source stays in the import directory. A failed
    step must not stop the next step. A lock that stays held blocks the
    title until the TTL of the lock expires. The lock goes first (#497).
    The S3 and file steps can take longer than the stop wait of
    supervisor. reservation is the name of the reservation. The default
    is the basename of the source. pending_archive is the run-scoped S3
    upload of the import (#430). It is deleted."""

    if lock:
        try:
            current_app.lock_manager.unlock(lock)
            current_app.logger.info(f"Removed lock {lock}")
        except Exception:
            current_app.logger.warning(traceback.format_exc())
    if pending_archive:
        try:
            discard_pending_archive(pending_archive)
        except Exception:
            current_app.logger.warning(traceback.format_exc())

    for stray in strays:
        if not stray:
            continue
        try:
            os.remove(stray)
        except FileNotFoundError:
            pass
        except OSError:
            current_app.logger.warning(traceback.format_exc())
    try:
        space.release_staging_reservation(reservation or os.path.basename(source_path))
    except Exception:
        current_app.logger.warning(traceback.format_exc())
    if not keep_source:
        try:
            filenames.move_to_rejects(source_path, reason)
        except Exception:
            current_app.logger.error(traceback.format_exc())


class HandoffFailed(Exception):
    """A stage could not queue its retry or its next stage (#521).

    The source is good. Only Redis failed. Thus, the handler of the
    stage keeps the source for the hourly sweep through abandon_handoff.
    It does not reject the source."""


def handoff(submit, *args, **kwargs):
    """Call an enqueue or a requeue. Raise HandoffFailed if it fails (#521)."""

    try:
        return submit(*args, **kwargs)
    except Exception as e:
        raise HandoffFailed(f"{type(e).__name__}: {e}") from e


def abandon_handoff(source_path, lock, staged_output, pending_archive=None):
    """Clean up after a stage could not queue the next stage (#340).

    Remove the localized output, release the staging reservation and the
    title lock, and leave the source in place. The hourly sweep of the
    import directory queues the source again. Without staging, the
    output is a hidden file in the library folder (#389). It goes too."""

    current_app.logger.error(
        f"'{os.path.basename(source_path)}' The next stage could not be "
        f"queued. The source stays for the next sweep: " + traceback.format_exc()
    )
    abandon_import(
        source_path,
        lock,
        strays=[staged_output],
        keep_source=True,
        pending_archive=pending_archive,
    )
