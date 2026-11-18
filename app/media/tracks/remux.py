"""The remux of a library file with mkvmerge, through the staging area."""

import errno
import hashlib
import os
import re
import shutil
import subprocess
from flask import current_app
from app.tasks import report_progress
from app.media.naming import hidden
from app.media import videos

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

    current_app.logger.info(f"'{name}' Running mkvmerge: {command}")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    watch_mkvmerge_progress(process, job, name, activity)
    videos.wait_for_subprocess(process, ok_returncodes=(0, 1))


class LibraryFileChanged(RuntimeError):
    """An other program changed a library file during a rewrite of it (#732, #741)."""

    def __init__(self, library_path):
        super().__init__(
            f"An other program changed '{library_path}' during the rewrite. "
            f"The rewrite stops, and the file stays as it is"
        )
        self.library_path = library_path


def library_stamp(library_path):
    """Return the size and the modification time of a library file."""

    library = os.stat(library_path)
    return {"size": library.st_size, "mtime": library.st_mtime}


def refuse_changed_library_file(stamp, library_path):
    """Raise LibraryFileChanged if a library file is not the file of the stamp.

    Invariant: a rewrite never replaces a library file that changed
    after the rewrite read it (#732, #741). The title lock stops each
    job of Fitzflix for the minutes or hours of a rewrite. It does not
    stop Radarr or Sonarr. They put an upgrade at the same path, and
    the import of that file waits for the lock. The copy that the
    rewrite works on is then the older release. Each rewrite takes
    library_stamp before it reads the file, and it calls this function
    before it writes.

    With no stamp, there is no check. An upgrade with a new quality in
    its name deletes the old file. Then the file is gone and its folder
    is there, and that is a change too. With no folder, the volume is
    away, and the OSError of the stat goes to the caller."""

    if not stamp:
        return
    try:
        now = library_stamp(library_path)
    except FileNotFoundError:
        if not os.path.isdir(os.path.dirname(library_path)):
            raise
        now = None
    if now != stamp:
        raise LibraryFileChanged(library_path)


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
    rewrite passes confirm_rewrite. That call stops the rename when the
    record went or moved during the remux (#726). It also marks the
    archive stale before the bytes change (#517). A raise of
    before_replace removes the result.

    The remux takes the stamp of the file before it reads it. A file
    that an other program changed since then is not replaced (#741).
    That check comes before before_replace. Thus, a file that the remux
    does not replace gets no stale flag."""

    from app.media.importing.space import release_staging_reservation, reserve_staging

    directory, basename = os.path.split(file_path)
    staging_dir = current_app.config["STAGING_DIR"]
    stamp = library_stamp(file_path)

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
            refuse_changed_library_file(stamp, file_path)
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
        videos.copy_with_progress(
            file_path, staged_source, job, name, "Copying to local staging"
        )
        _run_mkvmerge(build_command(staged_source, staged_output), job, name, activity)
        os.remove(staged_source)

        # The check comes before the long copy back, and again right
        # before the rename (#741).

        refuse_changed_library_file(stamp, file_path)
        videos.copy_with_progress(
            staged_output, hidden_library, job, name, "Moving into the library"
        )
        refuse_changed_library_file(stamp, file_path)
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
        _release_reservation_quietly(release_staging_reservation, reservation, name)


def _release_reservation_quietly(release, reservation, name):
    """Release a staging reservation. A fault only logs a warning (#596).

    The release runs in a finally after the rename into the library. A
    Redis fault there hid from the caller that the file changed. Then no
    scan was queued and no edit was counted. The "at" stamp of the
    reservation lets the grace sweep expire it."""

    try:
        release(reservation)
    except Exception as e:
        current_app.logger.warning(
            f"'{name}' Cannot release the staging reservation {reservation}: {e}"
        )


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
