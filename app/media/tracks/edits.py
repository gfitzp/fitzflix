"""The edits of the tracks of a file: flags, languages, order, and the removal of tracks."""

import json
import os
import subprocess
import traceback
from datetime import datetime, timedelta, timezone
from pymediainfo import MediaInfo
from rq import get_current_job
from flask import current_app
from werkzeug.local import LocalProxy
from app.tasks import report_progress, requeue, requeue_job_id
from app import db, get_app
from app.archive.uploads import archive_library_file
from app.archive.restores import refuse_while_restore_pending
from app.models import File
from app.settings import get_setting
from app.plex.plex_library import enqueue_plex_analyze
from app.media import videos
from app.media.tracks import remux, rewrite_guard, scan

app = LocalProxy(get_app)


def _locked_edit(
    task, file_id, args, queue, timeout, what, run, retry=None, kwargs=None
):
    """Run 1 track edit of file_id under the title lock of the file (#614).

    This is the 1 wrapper of the 3 edit tasks. It takes the title lock,
    or books a lock wait and returns True. A pending restore refuses the
    edit (#610). run() is the edit. Any raise of run() queues 1 track
    scan and raises again. A repeat of an edit reads the file again and
    rebuilds the rows (#611). A failure before the tool wrote costs 1
    scan that only reads the file. LibraryFileChanged queues no scan
    (#741). The import of the changed file does that work. The wrapper
    then puts the stale flag back to its value before the edit (#762).

    retry(error, name) can book a retry of a transient fault. It returns
    True if it did. Then the task returns False with no scan, because
    the retry rebuilds the rows. queue is a roster key. timeout names
    the config setting of the lock TTL and of the job. kwargs go to the
    lock wait."""

    with app.app_context():
        file = db.session.get(File, int(file_id))
        if file is None:
            return True
        name = f"'{file.basename}'"
        seconds = current_app.config[timeout]

        # The flag before the edit (#762). The lock below makes no commit,
        # so the row keeps the values of the read above. Each writer that
        # sets the flag holds the title lock, except the repair of a
        # replaced file (repair_replaced_file_task). A flag from it is
        # about bytes that an other program replaced already.

        stale_before = bool(file.aws_stale)

        # Serialize with the other tasks that rewrite the files or the
        # track records of this title.

        lock = videos.acquire_lock_or_defer(
            file.file_identifier(),
            seconds * 1000,
            queue,
            task,
            minutes=(5, 15),
            timeout=seconds,
            description=name,
            args=args,
            kwargs=kwargs,
            key=int(file_id),
        )
        if not lock:
            return True

        try:
            refused = refuse_while_restore_pending(file)
            if refused:
                current_app.logger.warning(f"{name} {refused} The {what} does not run")
                return False
            return run()
        except BaseException as error:
            try:
                booked = retry is not None and retry(error, name)
            except Exception:
                current_app.logger.warning(traceback.format_exc())
                booked = False
            if booked:
                return False
            current_app.logger.error(traceback.format_exc())

            # An other program put a different file at the path (#741).
            # Its import waits for this lock. A scan now would store the
            # size of that file on the record, and the import would then
            # take the file as cataloged already (already_cataloged). The
            # stale flag goes back to its value before the edit (#762).

            if isinstance(error, remux.LibraryFileChanged):
                rewrite_guard.put_stale_flag_back(int(file_id), stale_before, name)
            else:
                rewrite_guard._rescan_after_a_failed_edit(file_id, name)
            raise
        finally:
            current_app.lock_manager.unlock(lock)


def mkvpropedit_task(
    file_id,
    default_audio_track,
    default_subtitle_track,
    forced_subtitle_tracks,
    track_languages=None,
    transient_retries=0,
):
    """Update the MKV properties of a file.

    The tracks are Matroska track UIDs as text (#611). A remux keeps
    the UID of a track and changes its position. Thus, an edit that
    waits for the lock or for a retry still finds its track.
    default_subtitle_track "0" sets no default subtitle track.
    `track_languages` maps a track UID to the ISO 639-2 code to set.
    None (or an empty mapping) changes no language.
    """

    edit_args = (
        file_id,
        default_audio_track,
        default_subtitle_track,
        forced_subtitle_tracks,
        track_languages,
    )

    def run():
        return mkvpropedit_unlocked(*edit_args)

    def retry(error, name):
        """Book the edit again after a transient I/O fault of the share.

        The tracks are UIDs, so the same arguments are still valid. The
        retry takes the lock again, like a new run. The digest of the
        arguments keeps the retries of 2 different edits of this file
        apart (#595)."""

        if not (
            isinstance(error, OSError)
            and error.errno in videos.TRANSIENT_COPY_ERRNOS
            and transient_retries < videos.MAX_TRANSIENT_RETRIES
        ):
            return False
        counters = {"transient_retries": transient_retries + 1}
        requeue(
            "app.media.tracks.edits.mkvpropedit_task",
            key=int(file_id),
            delay=timedelta(minutes=5),
            reason=(
                f"MKV property edit failed with a transient I/O error "
                f"({error}) (attempt {transient_retries + 1} of "
                f"{videos.MAX_TRANSIENT_RETRIES})"
            ),
            counters=counters,
            args=edit_args,
            description=name,
            job_id=requeue_job_id(
                "app.media.tracks.edits.mkvpropedit_task",
                int(file_id),
                counters,
                tag=videos.arguments_tag(edit_args),
            ),
        )
        return True

    return _locked_edit(
        "app.media.tracks.edits.mkvpropedit_task",
        file_id,
        edit_args,
        "file",
        "MKVPROPEDIT_TASK_TIMEOUT",
        "MKV property edit",
        run,
        retry=retry,
        kwargs={"transient_retries": transient_retries},
    )


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
    audio_tracks,
    subtitle_tracks,
    default_audio_track,
    default_subtitle_track,
    forced_subtitle_tracks,
    track_languages=None,
):
    """Return the mkvpropedit arguments that set the flags and the languages.

    The tracks are the track dicts of a new read of the file. The
    selections are track UIDs as text (#611). "--edit track:=UID"
    selects a track by its UID, not by its position. Each audio track
    gets its default flag. The subtitle tracks get their default and
    forced flags only when the caller selected a default or a forced
    track. The language corrections are independent: a file whose
    flags are correct still needs them. mkvpropedit also applies a
    language to LanguageIETF."""

    args = []
    for track in audio_tracks:
        flag = 1 if track["uid"] == default_audio_track else 0
        args += ["--edit", f"track:={track['uid']}", "--set", f"flag-default={flag}"]
    if default_subtitle_track or forced_subtitle_tracks:
        forced_uids = set(forced_subtitle_tracks or ())
        for track in subtitle_tracks:
            default = 1 if track["uid"] == default_subtitle_track else 0
            forced = 1 if track["uid"] in forced_uids else 0
            selector = f"track:={track['uid']}"
            args += ["--edit", selector, "--set", f"flag-default={default}"]
            args += ["--edit", selector, "--set", f"flag-forced={forced}"]
    for track in list(audio_tracks) + list(subtitle_tracks):
        language = (track_languages or {}).get(track["uid"])
        if language:
            args += [
                "--edit",
                f"track:={track['uid']}",
                "--set",
                f"language={language}",
            ]
    return args


def missing_track_uids(
    audio_tracks,
    subtitle_tracks,
    default_audio_track,
    default_subtitle_track,
    forced_subtitle_tracks,
    track_languages=None,
):
    """Return the selected track UIDs that the file does not have (#611).

    The tracks are the track dicts of a new read of the file. A UID
    that is not there means that the file changed after the page was
    loaded. A track of the file with no UID also counts. Then no
    selection can address it. The result is a sorted list."""

    audio = {track.get("uid") for track in audio_tracks}
    subtitles = {track.get("uid") for track in subtitle_tracks}
    missing = {None} & (audio | subtitles)
    if default_audio_track is not None and default_audio_track not in audio:
        missing.add(default_audio_track)
    if default_subtitle_track not in (None, "0") and (
        default_subtitle_track not in subtitles
    ):
        missing.add(default_subtitle_track)
    missing |= set(forced_subtitle_tracks or ()) - subtitles
    missing |= set(track_languages or {}) - audio - subtitles
    return sorted("no UID" if uid is None else str(uid) for uid in missing)


def mkvpropedit_unlocked(
    file_id,
    default_audio_track,
    default_subtitle_track,
    forced_subtitle_tracks,
    track_languages=None,
):
    """Update the MKV properties of a file. The caller must hold the lock of the title.

    A raise leaves the rescan to the caller (#614)."""

    with app.app_context():
        name = f"File {file_id}"

        try:
            job = get_current_job()

            # Get the record of the file to modify

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            name = f"'{file.basename}'"

            report_progress(job, file.basename, "Updating MKV properties")

            audio_tracks = scan.get_audio_tracks_from_file(file_path)
            subtitle_tracks = scan.get_subtitle_tracks_from_file(file_path)

            current_app.logger.info(
                f"{name} file_id {file_id}: default audio {default_audio_track}, "
                f"default subtitle {default_subtitle_track}, forced subtitles "
                f"{forced_subtitle_tracks}, languages {track_languages}"
            )

            # A UID that the file does not have means that the file changed
            # after the page was loaded. The edit stops before it writes,
            # and a scan reads the rows again (#611).

            missing = missing_track_uids(
                audio_tracks,
                subtitle_tracks,
                default_audio_track,
                default_subtitle_track,
                forced_subtitle_tracks,
                track_languages,
            )
            if missing:
                return rewrite_guard._stop_on_changed_file(
                    file_id, name, missing, "edit"
                )

            localization_arguments = build_propedit_args(
                audio_tracks,
                subtitle_tracks,
                default_audio_track,
                default_subtitle_track,
                forced_subtitle_tracks,
                track_languages,
            )

            current_app.logger.info(
                f"{file.basename} localization_arguments: {localization_arguments}"
            )

            if localization_arguments:
                # mkvpropedit writes the file in place (#517). The check of
                # the record and the flag come before the tool starts. A
                # failure of one of them stops the edit before its first
                # byte (#615, #726).

                rewrite_guard.confirm_rewrite(file_id, file_path)
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
                try:
                    for line in mkvpropedit_task.stdout:
                        line = line.replace("\n", "")
                        current_app.logger.info(f"'{file.basename}' {line}")
                except BaseException:
                    # The child must not keep writing after a fault (#499).
                    remux.stop_process(mkvpropedit_task)
                    raise

                videos.wait_for_subprocess(mkvpropedit_task, ok_returncodes=(0, 1))

                # If the default audio track is not the first track, make a
                # new file with the default audio track first. Then Plex
                # selects it first

                if (
                    default_audio_track is not None
                    and default_audio_track != audio_tracks[0]["uid"]
                ):
                    new_track_order = track_order(MediaInfo.parse(file_path).tracks)

                    current_app.logger.info(
                        f"{file.basename} new_track_order: {new_track_order}"
                    )

                    # The remux runs on staging and replaces the file at
                    # the end (#298).

                    remux.remux_file(
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
                        before_replace=lambda: rewrite_guard.confirm_rewrite(
                            file_id, file_path
                        ),
                    )

            # Remove the subtitle tracks that have zero elements. The
            # removal renumbers the subtitle tracks that stay (#582)

            remove_empty_subtitle_tracks(
                file_path,
                before_replace=lambda: rewrite_guard.confirm_rewrite(
                    file_id, file_path
                ),
            )

            # Build the audio and subtitle track info again after the changes

            output_audio_tracks = scan.get_audio_tracks_from_file(file_path)
            output_subtitle_tracks = scan.get_subtitle_tracks_from_file(file_path)

            # The file work can take 30 minutes (a remux). A transaction
            # from before it fails its writes with MariaDB error 1020 if a
            # triage click or an upload changed the row. A delete of the
            # track rows before it held their locks for the whole remux,
            # and a new import waited past the lock timeout. Thus, the
            # writes start a new transaction here.

            file = rewrite_guard._fresh_file(file.id)
            scan.replace_track_rows(file, output_audio_tracks, output_subtitle_tracks)

            rewrite_guard.record_filesize(file, os.path.getsize(file_path))
            file.date_updated = datetime.now(timezone.utc)
            db.session.commit()

        except Exception:
            db.session.rollback()
            raise

        else:
            # The file on disk changed. The default flags changed. After a
            # reorder, the whole track layout changed. Plex reads it again
            # now, not at its next scan

            enqueue_plex_analyze(file_path)

        # The edited library file goes to modified/, or to transcoded/
        # for a conversion (#299). The rip in untouched/ never changes. A
        # failed upload leaves the stale flag of the write in place. The
        # repair queue then uploads the file (#615).

        if get_setting("ARCHIVE_ORIGINAL_MEDIA"):
            try:
                archive_library_file(file, file_path)
            except Exception:
                db.session.rollback()
                raise
            db.session.commit()

        return True


def mkvmerge_identify(file_path):
    """Return the track list of mkvmerge -J for a file.

    mkvmerge -J reads only the headers. Thus, this is fast on a large
    file."""

    result = subprocess.run(
        [current_app.config["MKVMERGE_BIN"], "-J", file_path],
        capture_output=True,
        text=True,
    )
    videos.wait_for_subprocess(result, ok_returncodes=(0, 1))
    return json.loads(result.stdout).get("tracks") or []


def mkvmerge_track_ids(file_path):
    """Return the mkvmerge track ids of a file by track UID (#611).

    The result maps "audio" and "subtitles" to a dict of UID (text) to
    track id. mkvmerge -a and -s take these ids, not UIDs."""

    ids = {"audio": {}, "subtitles": {}}
    for track in mkvmerge_identify(file_path):
        uid = (track.get("properties") or {}).get("uid")
        if track.get("type") in ids and uid is not None:
            ids[track["type"]][str(uid)] = int(track["id"])
    return ids


def mkvmerge_task(file_id, audio_tracks, subtitle_tracks):
    """Remux an MKV file with only the given tracks.

    The tracks are the Matroska track UIDs to keep, as text (#611). The
    remux maps them to the track ids of the file after it takes the
    lock."""

    return _locked_edit(
        "app.media.tracks.edits.mkvmerge_task",
        file_id,
        (file_id, audio_tracks, subtitle_tracks),
        "import",
        "MKVPROPEDIT_TASK_TIMEOUT",
        "remux",
        lambda: mkvmerge_unlocked(file_id, audio_tracks, subtitle_tracks),
    )


def mkvmerge_unlocked(file_id, audio_tracks, subtitle_tracks):
    """Remux an MKV file. The caller must hold the lock of the title.

    A raise leaves the rescan to the caller (#614)."""

    with app.app_context():
        name = f"File {file_id}"

        try:
            job = get_current_job()

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            name = f"'{file.basename}'"

            # The form gives the UID of each kept track (#611). mkvmerge
            # selects the tracks by their ids in the file. A new read of
            # the file maps each UID to its id now, under the lock.

            ids = mkvmerge_track_ids(file_path)
            missing = sorted(
                ({str(uid) for uid in audio_tracks} - set(ids["audio"]))
                | ({str(uid) for uid in subtitle_tracks} - set(ids["subtitles"]))
            )
            if missing:
                return rewrite_guard._stop_on_changed_file(
                    file_id, name, missing, "remux"
                )

            audio_tracks = sorted(ids["audio"][str(uid)] for uid in audio_tracks)
            subtitle_tracks = sorted(
                ids["subtitles"][str(uid)] for uid in subtitle_tracks
            )

            current_app.logger.info(
                f"{name} Keeps audio track ids {audio_tracks} and subtitle "
                f"track ids {subtitle_tracks}"
            )

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

            remux.remux_file(
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
                before_replace=lambda: rewrite_guard.confirm_rewrite(
                    file_id, file_path
                ),
            )

            # Remove the subtitle tracks that have zero elements

            remove_empty_subtitle_tracks(
                file_path,
                before_replace=lambda: rewrite_guard.confirm_rewrite(
                    file_id, file_path
                ),
            )

            # Build the audio and subtitle track info again after the changes

            output_audio_tracks = scan.get_audio_tracks_from_file(file_path)
            output_subtitle_tracks = scan.get_subtitle_tracks_from_file(file_path)

            # The file work can take 30 minutes (a remux). A transaction
            # from before it fails its writes with MariaDB error 1020 if a
            # triage click or an upload changed the row. A delete of the
            # track rows before it held their locks for the whole remux,
            # and a new import waited past the lock timeout. Thus, the
            # writes start a new transaction here.

            file = rewrite_guard._fresh_file(file.id)
            scan.replace_track_rows(file, output_audio_tracks, output_subtitle_tracks)
            db.session.commit()

        except Exception:
            db.session.rollback()
            raise

        # This task already holds the lock of the title. Thus, call the
        # unlocked variant directly. A call to the locked variant would
        # deadlock against this task. The first audio track that stays
        # becomes the default.

        first_audio = output_audio_tracks[0]["uid"] if output_audio_tracks else None
        mkvpropedit_unlocked(file.id, first_audio, None, None)
        return True


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

    remux.remux_file(
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
