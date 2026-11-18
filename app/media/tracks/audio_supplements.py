"""The second audio tracks that Fitzflix adds for players that cannot decode a lossless track."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import traceback
from datetime import datetime, timezone
from pymediainfo import MediaInfo
from rq import get_current_job
from flask import current_app
from werkzeug.local import LocalProxy
from app.tasks import report_progress
from app import db, get_app
from app.archive.uploads import archive_library_file
from app.models import File
from app.media.naming import hidden
from app.settings import get_setting
from app.plex.plex_library import enqueue_plex_analyze
from app.media import videos
from app.media.tracks import edits, remux, rewrite_guard, scan
from app.media.importing import space
from app.media.importing.space import set_staging_reservation

app = LocalProxy(get_app)


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

    The percent stops at 99. A stream can run past the duration
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


def supplement_lossless_tracks(file_path):
    """Give each lossless non-FLAC/PCM audio track a FLAC twin before it.

    The original stays. The twin plays natively on Apple TV clients.
    The lossless original stays for direct play and future
    passthrough. Some files already have their twins. These are MakeMKV
    rips made with the "FLAC Plus Original Audio" profile, or downloads
    of uploads that already had supplements. These files plan as pure
    copies, and the pass does not change them. Thus, the pass is safe
    to repeat.
    """

    with app.app_context():
        temp_flac_file = None
        flac_track_process = None
        try:
            job = get_current_job()

            dirname = os.path.dirname(file_path)
            basename = os.path.basename(file_path)

            audio_tracks = scan.get_audio_tracks_from_file(file_path)
            if not audio_tracks:
                return True

            plan = plan_audio_supplements(audio_tracks)
            conversions = sum(1 for action, _ in plan if action == "flac")
            if not conversions:
                # A file with only FLAC, PCM, or lossy audio also comes
                # here. Thus, the line does not say that twins exist.
                current_app.logger.info(
                    f"'{basename}' No audio track needs a FLAC twin"
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

            videos.wait_for_subprocess(flac_track_process)
            copy_image_attachments(file_path, temp_flac_file)

            current_app.logger.info(f"'{basename}' Supplemented lossless tracks")
            current_app.logger.info(f"Moving '{temp_flac_file}' to '{file_path}'")
            shutil.move(temp_flac_file, file_path)

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
    for a conversion. The rip in untouched/ never changes.
    """

    return edits._locked_edit(
        "app.media.tracks.audio_supplements.remux_audio_plan_task",
        file_id,
        (int(file_id), plan),
        "transcode",
        "TRANSCODE_TASK_TIMEOUT",
        "audio remux",
        lambda: _remux_audio_plan_unlocked(int(file_id), plan),
    )


def _remux_audio_plan_unlocked(file_id, plan):
    """Run the remux pipeline. The caller must hold the lock of the title."""

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
        # remux_file. 2 extras named "Trailer.mkv" can run at the
        # same time on the transcode queue.
        staged_name = f"{hashlib.sha1(file_path.encode()).hexdigest()[:8]}-{basename}"
        staging_source = os.path.join(staging_dir, f".src-{staged_name}")
        staging_output = os.path.join(staging_dir, staged_name)
        reservation = f"audio-plan:{staged_name}"
        try:
            # The reservation sees the space that the imports reserved, and
            # the imports see this one. A busy reservation lock
            # falls back to the plain check of the free space.

            source_bytes = os.path.getsize(file_path)
            needed = int(source_bytes * remux.STAGED_REMUX_FACTOR)
            for _ in range(remux.REMUX_RESERVATION_TRIES):
                status = space.reserve_staging(
                    reservation,
                    needed,
                    [f"src-{staged_name}", staged_name],
                    staging_dir,
                )
                if status != "lock":
                    break
            if status == "lock":
                current_app.logger.warning(
                    f"'{basename}' The staging reservation lock stays busy. "
                    f"Checking the free space only"
                )
                if shutil.disk_usage(staging_dir).free < needed + 16 * 2**30:
                    raise RuntimeError(f"'{basename}' not enough staging space")
            elif status != "ok":
                raise RuntimeError(f"'{basename}' not enough staging space")

            stamp = remux.library_stamp(file_path)
            videos.copy_with_progress(
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

            audio_tracks = scan.get_audio_tracks_from_file(staging_source)
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

            new_audio_tracks = scan.get_audio_tracks_from_file(staging_output)
            new_subtitle_tracks = scan.get_subtitle_tracks_from_file(staging_output)
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
            # name does not reach the key. The first run took the
            # key from the name. It uploaded 40 GB under 'untouched/src-…'
            # (the Fellowship incident).

            os.remove(staging_source)
            # The source copy is gone. The reservation shrinks to the
            # output, which waits for the upload.
            set_staging_reservation(reservation, needed - source_bytes, [staged_name])
            final_staging = staging_output

            # A failed copy leaves a partial hidden file next to the
            # library file. Remove it, as the other write-backs do.

            hidden_library = hidden(os.path.join(os.path.dirname(file_path), basename))
            try:
                remux.refuse_changed_library_file(stamp, file_path)
                videos.copy_with_progress(
                    final_staging,
                    hidden_library,
                    job,
                    basename,
                    "Moving into the library",
                )
                remux.refuse_changed_library_file(stamp, file_path)
                rewrite_guard.confirm_rewrite(int(file_id), file_path)
                os.replace(hidden_library, file_path)
            except BaseException:
                if os.path.exists(hidden_library):
                    os.remove(hidden_library)
                raise

            # Build the track records again because the file changed. A new
            # transaction avoids MariaDB error 1020 after the long remux.
            # The file has the new layout now. A failed commit raises, and
            # the task wrapper queues a scan.

            try:
                file = rewrite_guard._fresh_file(file.id)
                scan.replace_track_rows(file, new_audio_tracks, new_subtitle_tracks)
                rewrite_guard.record_filesize(file, os.path.getsize(final_staging))
                file.date_updated = datetime.now(timezone.utc)
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise

            # The audio layout is new. Plex reads it again now, not at its
            # own pace

            enqueue_plex_analyze(file_path)

            # The rebuilt library file goes to modified/, or to transcoded/
            # for a conversion. The upload reads the staging copy.
            # A failure leaves the stale flag of the write in place. Then
            # the S3 sync uploads it again.

            if get_setting("ARCHIVE_ORIGINAL_MEDIA"):
                try:
                    archive_library_file(file, final_staging)
                except Exception:
                    db.session.rollback()
                    raise
            return True

        finally:
            for stray in (staging_source, staging_output):
                try:
                    os.remove(stray)
                except OSError:
                    pass
            remux._release_reservation_quietly(
                space.release_staging_reservation, reservation, basename
            )
