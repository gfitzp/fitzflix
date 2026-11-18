"""The second job of an import: copy the localized file into the library."""

import os
import shutil
import subprocess
import traceback
from datetime import timedelta
from rq import get_current_job
from flask import current_app
from werkzeug.local import LocalProxy
from app.tasks import enqueue_task, requeue
from app import get_app, makedirs_on_mounted_volume
from app.archive.archive_keys import pending_archive_of
from app.settings import get_setting
from app.email import notify_admin
from app.media.naming import hidden
from app.media.sdr_conversion import needs_placebo, plan_conversion
from app.media.tracks.scan import (
    _extract_media_details,
    get_audio_tracks_from_file,
    get_subtitle_tracks_from_file,
)
from app.media.tracks.languages import same_language
from app.media.tracks.edits import remove_empty_subtitle_tracks
from app.media.tracks.remux import stop_process
from app.media import videos
from app.media.importing import cataloging, chain, filenames, localization, space
from app.operations.maintenance import clear_leftover_directory
from app.arr.radarr_push import enqueue_radarr_push
from app.arr.radarr_push import radarr_configured

app = LocalProxy(get_app)


def _plan_conversion_or_reject(file_path, file_details, max_height):
    """Return the conversion plan of an import, None, or False after a reject.

    A conversion needs ARCHIVE_ORIGINAL_MEDIA. The library gets only the
    conversion. Thus, without the archive, Fitzflix would lose the only
    copy of the original. A Dolby Vision Profile 5 file needs
    FFMPEG_PLACEBO_BIN. Each of these rejects the file and tells the admin."""

    basename = os.path.basename(file_path)
    try:
        plan = plan_conversion(file_path, file_details, max_height)
    except ValueError as e:
        reason = str(e)
        plan = None
    else:
        if not plan:
            return None
        if not get_setting("ARCHIVE_ORIGINAL_MEDIA"):
            reason = (
                "A conversion needs ARCHIVE_ORIGINAL_MEDIA. Without the archive, "
                "the original would be lost"
            )
        elif needs_placebo(plan) and not current_app.config["FFMPEG_PLACEBO_BIN"]:
            reason = "A Dolby Vision Profile 5 conversion needs FFMPEG_PLACEBO_BIN"
        else:
            return plan

    # The notification is the alert. An ERROR record would mail the admin
    # a second time.
    current_app.logger.warning(f"'{basename}' rejected: {reason}")
    filenames.move_to_rejects(file_path, "conversion not possible")
    notify_admin(
        "A conversion is not possible",
        "conversion_rejected",
        basename=basename,
        reason=reason,
        rejects_directory=current_app.config["REJECTS_DIR"],
    )
    return False


def inspect_localized_file(file_path, container, job=None):
    """Apply the final track flags to a localized file and report its details.

    This function runs where the file is: on local staging, before the
    library copy. Thus, the flag edits and the parse never occur on the
    sql queue. The first audio track becomes the only default. The first
    subtitle track becomes the default if the audio is foreign. This
    function removes the empty subtitle tracks. It returns the media
    details that finalize_localization needs.
    """

    name = os.path.basename(file_path)

    if container == "Matroska":
        audio_tracks = get_audio_tracks_from_file(file_path)
        subtitle_tracks = get_subtitle_tracks_from_file(file_path)

        # Set the first audio track as the only default audio track.

        if len(audio_tracks) >= 1:
            current_app.logger.info(
                f"'{name}' Setting the first audio track as the only default"
            )

            audio_flag_args = [
                "--edit",
                "track:a1",
                "--set",
                "flag-default=1",
            ]

            if audio_tracks[0].get("language") == "und":
                audio_flag_args.extend(["--edit", "track:a1", "--set", "language=und"])

            # Clear the default flag from every other audio track. Then a
            # player does not select unpredictably between multiple defaults.

            for track_number in range(2, len(audio_tracks) + 1):
                audio_flag_args.extend(
                    [
                        "--edit",
                        f"track:a{track_number}",
                        "--set",
                        "flag-default=0",
                    ]
                )

            mkvpropedit_process = subprocess.Popen(
                [current_app.config["MKVPROPEDIT_BIN"], file_path] + audio_flag_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )
            try:
                for line in mkvpropedit_process.stdout:
                    current_app.logger.info(f"'{name}' {line.rstrip()}")
            except BaseException:
                stop_process(mkvpropedit_process)
                raise

            videos.wait_for_subprocess(mkvpropedit_process, ok_returncodes=(0, 1))

        # The 2 spellings of a dual-code language are the same language.

        native_language = current_app.config["NATIVE_LANGUAGE"]

        # Set the first subtitle track as the default if the first audio is
        # foreign and if there is no default subtitle track.

        existing_default_subtitle_track = any(
            track["default"] == True for track in subtitle_tracks
        )

        if (
            len(subtitle_tracks) >= 1
            and len(audio_tracks) >= 1
            and not same_language(audio_tracks[0].get("language"), native_language)
            and audio_tracks[0].get("language") != "und"
            and not existing_default_subtitle_track
        ):
            current_app.logger.info(
                f"'{name}' Setting the first subtitle track as default"
            )
            mkvpropedit_process = subprocess.run(
                [
                    current_app.config["MKVPROPEDIT_BIN"],
                    file_path,
                    "--edit",
                    "track:s1",
                    "--set",
                    "flag-default=1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            for line in mkvpropedit_process.stdout.splitlines():
                current_app.logger.info(f"'{name}' {line.rstrip()}")

            videos.wait_for_subprocess(mkvpropedit_process, ok_returncodes=(0, 1))

        # Remove the subtitle tracks that have 0 elements.

        remove_empty_subtitle_tracks(file_path)

    return _extract_media_details(file_path)


def move_localized_file(
    source_path,
    file_details,
    lock,
    hidden_output_file,
    transient_retries=0,
    space_waits=0,
    vanished_retries=0,
    lock_waits=0,
):
    """Carry the localized output to a hidden name at its library destination.

    This is the long file copy. It is split out of finalize_localization.
    Thus, it runs on the file-operation queue. Several copies can run in
    parallel. The single-worker sql queue only sees the quick database
    work and an immediate same-volume rename. The title lock passes
    through to finalize.

    A library volume without space for the copy makes the task wait. The
    output stays on staging, and the task keeps the title lock.

    After a copy to another volume, the output stays on staging until
    finalize_localization renames the copy into place. An unstable NFS
    mount can lose the just-written copy (the Movies share, Oct 2026).
    Then finalize gives the copy back to this task. vanished_retries
    counts those returns.

    If another task holds the title lock, the copy waits. Then
    neither this task nor finalize writes the files of the title.
    """

    with app.app_context():
        basename = file_details.get("basename")
        output_directory = os.path.join(
            current_app.config["LIBRARY_DIR"], file_details.get("dirname")
        )

        # Every retry of the copy carries all 4 counters. Each
        # retry changes only its own counter.

        move_args = (source_path, file_details, lock, hidden_output_file)
        counters = {
            "transient_retries": transient_retries,
            "space_waits": space_waits,
            "vanished_retries": vanished_retries,
            "lock_waits": lock_waits,
        }

        def retry(delay, reason, level="warning", **changes):
            localization.handoff(
                requeue,
                "app.media.importing.library_move.move_localized_file",
                key=source_path,
                delay=delay,
                reason=reason,
                counters={**counters, **changes},
                args=move_args,
                description=f"'{basename}'",
                level=level,
            )
            return False

        def defer():
            localization.handoff(
                chain.defer_for_title_lock,
                "app.media.importing.library_move.move_localized_file",
                move_args,
                counters,
                source_path,
                basename,
            )
            return False

        destination_hidden = hidden(os.path.join(output_directory, basename))

        def wait_for_space(needed, shortfall, directory=None):
            """Book the copy again after SPACE_WAIT. Keep the title lock.

            If another task holds the title lock, the copy waits for the
            lock instead."""

            if not localization.handoff(chain._hold_title_lock, lock, basename):
                return defer()
            space._log_space_wait(
                basename, directory or output_directory, needed, shortfall, space_waits
            )

            # The waiting retry names the localized file. Thus, the weekly
            # orphan sweep keeps it.

            return retry(
                space.SPACE_WAIT,
                "waits for space",
                level=None,
                space_waits=space_waits + 1,
            )

        # Each booking goes through handoff: the lock extension, the lock
        # defer, the volume retry, the space wait and the next stage. Thus,
        # the 1 HandoffFailed handler below cleans up after each of them.

        staged_output = None
        try:
            if not localization.handoff(chain._hold_title_lock, lock, basename):
                return defer()

            # Defer if a necessary volume is dead. The task keeps the title lock
            # for the retry.

            dead = videos._dead_volumes(
                [
                    output_directory,
                    os.path.dirname(hidden_output_file),
                    os.path.dirname(source_path),
                ]
            )
            if dead:
                return retry(
                    timedelta(minutes=5),
                    f"Volumes unavailable ({', '.join(dead)}) for the library copy",
                )

            # A copy to another volume needs space there. Check before the
            # inspection and the copy. Thus, a wait repeats neither.

            # A stat error skips the check. Then the copy meets the same
            # error, and the existing error handling applies.

            try:
                crosses_volumes = hidden_output_file != destination_hidden and (
                    space._crosses_volumes(hidden_output_file, output_directory)
                )
                needed = os.path.getsize(hidden_output_file) + space.SPACE_RESERVE
            except OSError:
                crosses_volumes = False
            if crosses_volumes:
                shortfall = space.library_space_shortfall(output_directory, needed)
                if shortfall:
                    return wait_for_space(needed, shortfall)
            if space_waits:
                current_app.logger.info(
                    f"'{basename}' The space is available again after "
                    f"{space_waits} wait(s). Copying"
                )

            job = get_current_job()

            # The final flag edits and the metadata extraction occur here,
            # while the file is still on local staging. Thus, the sql queue
            # never opens the file.

            # The removal of an empty subtitle track remuxes the output in
            # place. That needs a second copy on staging, outside the
            # reservation. An import must never go to rejects for space.
            # Thus, a failure with too little free space for that
            # copy waits like a copy into a full library. The failed remux
            # removes its partial file before this check.

            try:
                inspection = inspect_localized_file(
                    hidden_output_file, file_details.get("container"), job
                )
            except (subprocess.CalledProcessError, OSError):
                work_dir = os.path.dirname(hidden_output_file)
                needed = os.path.getsize(hidden_output_file) + space.SPACE_RESERVE
                free = shutil.disk_usage(work_dir).free
                if free < needed:
                    return wait_for_space(needed, needed - free, directory=work_dir)
                raise

            try:
                makedirs_on_mounted_volume(output_directory)

                if hidden_output_file == destination_hidden:
                    # A file too big for staging was localized at its
                    # destination name already.
                    pass

                elif (
                    os.stat(os.path.dirname(hidden_output_file)).st_dev
                    == os.stat(output_directory).st_dev
                ):
                    videos._rename_with_retries(hidden_output_file, destination_hidden)

                else:
                    # Keep the output on staging. finalize_localization
                    # removes it after the rename into place. If the mount
                    # loses the copy first, finalize copies it again from
                    # here.

                    videos.copy_with_progress(
                        hidden_output_file, destination_hidden, job, basename
                    )
                    staged_output = hidden_output_file

            except OSError as e:
                if e.errno in space.SPACE_ERRNOS:
                    # The volume filled during the copy, for example from
                    # another import. The output is intact on staging.
                    # Remove the partial copy and wait. Never reject the
                    # file for space.

                    try:
                        os.remove(destination_hidden)
                    except OSError:
                        pass
                    needed = os.path.getsize(hidden_output_file) + space.SPACE_RESERVE
                    return wait_for_space(
                        needed,
                        space.library_space_shortfall(output_directory, needed)
                        or needed,
                    )

                if (
                    e.errno not in videos.TRANSIENT_COPY_ERRNOS
                    or transient_retries >= videos.MAX_TRANSIENT_RETRIES
                ):
                    raise

                # An unstable mount interrupted the library copy. But the
                # localized output is still intact on staging. Remove the
                # partial destination and retry only this copy. Do not reject
                # the file and do the whole import again. The task keeps the
                # title lock for the retry.

                try:
                    os.remove(destination_hidden)
                except OSError:
                    pass
                return retry(
                    timedelta(minutes=5),
                    f"Library copy failed with a transient I/O error ({e}) "
                    f"(attempt {transient_retries + 1} of {videos.MAX_TRANSIENT_RETRIES})",
                    transient_retries=transient_retries + 1,
                )

            # The next stage gets the title lock. A failed enqueue must not
            # keep the lock for its full TTL, or leave the copies behind.

            localization.handoff(
                enqueue_task,
                "app.media.importing.cataloging.finalize_localization",
                source_path,
                file_details,
                lock,
                destination_hidden,
                inspection,
                staged_output=staged_output,
                vanished_retries=vanished_retries,
                description=f"'{basename}'",
            )

        except localization.HandoffFailed:
            # A booking failed. Only Redis failed. The source is
            # good. It stays for the hourly sweep, and the lock and the
            # copies go.

            cataloging._remove_staged_output(destination_hidden)
            localization.abandon_handoff(
                source_path,
                lock,
                hidden_output_file,
                pending_archive=pending_archive_of(file_details),
            )
            return False

        except BaseException as error:
            # Remove both hidden copies. The source has its original
            # bytes, unless the localization ran in place on a file that
            # was too large for staging. Fitzflix rejects the
            # source. A stop signal or an outage of the database keeps it.

            localization.abandon_failed_stage(
                error,
                source_path,
                lock,
                later_stage=True,
                strays=(hidden_output_file, destination_hidden),
                pending_archive=pending_archive_of(file_details),
            )

        return True


def _report_import_move_to_radarr(source_directory, output_directory):
    """Tell Radarr when an import left its folder for a different one.

    The source must be below a Radarr root folder, and the output must
    be a different folder. Then Radarr gets the new path and a rescan,
    and the empty folder of the download goes away. The Radarr update
    runs as its own job on the request queue. Thus, the sql queue never
    waits on Radarr. A Radarr failure is logged. It does not
    fail the import. The file is already in place."""

    source = os.path.realpath(source_directory)
    output = os.path.realpath(output_directory)
    if source == output or not radarr_configured():
        return
    roots = [os.path.realpath(r) for r in current_app.config["RADARR_ROOT_FOLDERS"]]
    if not any(os.path.commonpath([source, root]) == root for root in roots):
        return
    enqueue_radarr_push(
        "app.arr.radarr_push.follow_import_move_task",
        source,
        os.path.basename(output),
        description=f"Reporting an import move to Radarr ({os.path.basename(output)})",
    )
    try:
        clear_leftover_directory(source)
    except Exception:
        current_app.logger.warning(traceback.format_exc())
