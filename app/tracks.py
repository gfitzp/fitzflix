"""Track surgery (the strangler split from app.videos): everything
that reads or rewrites the streams inside a media file.

MediaInfo scanning and the stored track metadata (including Dolby
Vision profile parsing), the mkvpropedit flag editor and the
mkvmerge remuxer with their lock-holding task wrappers, subtitle
inspection (possibly-forced flagging, empty-track dropping), and the
lossless-audio supplement planner and remuxers.

app.videos re-exports every name here, so stored rq job strings
("app.videos.mkvmerge_task") and import sites keep resolving; the
shared lock/subprocess/copy plumbing stays in app.videos and is
imported lazily inside functions, keeping the module import direction
one-way.
"""

import os
import random
import re
import shutil
import subprocess
import traceback

from datetime import datetime, timedelta, timezone
from functools import lru_cache

from pymediainfo import MediaInfo
from rq import get_current_job

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app, retry_job_id, safe_job_id
from app.aws_storage import aws_upload
from app.models import File, FileAudioTrack, FileSubtitleTrack
from app.plex_library import enqueue_plex_analyze


def watch_mkvmerge_progress(process, job, name, activity):
    """Stream a process's output, logging its progress and updating job meta."""

    previous_percent = None
    for line in process.stdout:
        progress_match = re.search(r"Progress\: \d+\%", line)
        if progress_match:
            progress_match = re.match(r"^Progress\: (?P<percent>\d+)\%", line)
            percent = int(progress_match.group("percent"))
            if previous_percent != percent:
                current_app.logger.info(f"'{name}' {activity}: {percent}%")
                previous_percent = percent
            if job:
                job.meta["description"] = f"'{name}' — {activity}"
                job.meta["progress"] = percent
                job.save_meta()


def parse_dolby_vision_profile(hdr_format):
    """The Dolby Vision flavor ("5", "7", "8.1", …) from MediaInfo's
    combined HDR-format string, or None when the video isn't DV.

    The profile number rides in the codec-profile token (dvhe.08.06 →
    profile 8; dvhe and dvh1 are HEVC, dvav/dva1 AVC, dav1 AV1). For
    profile 8 the meaningful flavor is the cross-compatibility target,
    which MediaInfo reports as compatibility text in the same string:
    HDR10-compatible is 8.1, HLG 8.4, plain-SDR 8.2.
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
    """Parse a file and return the media details its database records need.

    Everything here is plain data — video track fields, audio and subtitle
    track dicts, and the file size — so the sql-queue tasks that write the
    records never have to open the file themselves.
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

    # HDR fields are always present — None when absent — so a rescan
    # of a replaced file CLEARS stale values instead of keeping them

    video["hdr_format"] = None
    for track in media_info.tracks:
        if track.track_type == "Video" and track.other_hdr_format:
            if track.other_hdr_format[0]:
                video["hdr_format"] = track.other_hdr_format[0]
                break
    video["dolby_vision_profile"] = parse_dolby_vision_profile(video["hdr_format"])

    return {
        "video": video,
        "audio_tracks": get_audio_tracks_from_file(file_path),
        "subtitle_tracks": get_subtitle_tracks_from_file(file_path),
        "filesize_bytes": os.path.getsize(file_path),
    }


def extract_track_metadata(file_path):
    """Drop empty subtitle tracks and report a library file's media details.

    The file half of a track rescan; the returned details feed
    save_track_metadata on the sql queue.
    """

    media_info = MediaInfo.parse(file_path)
    for track in media_info.tracks:
        if track.track_type == "General" and track.format == "Matroska":
            remove_empty_subtitle_tracks(file_path)
            break

    return _extract_media_details(file_path)


def track_metadata_scan_library():
    """Add all files in the library to the metadata scan queue."""

    with app.app_context():
        try:

            files = File.query.all()
            for file in files:
                current_app.file_queue.enqueue(
                    "app.videos.track_metadata_scan_task",
                    args=(file.id,),
                    job_timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
                    description=f"{file.basename} – Scanning track metadata",
                )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            raise

        return True


def track_metadata_scan_task(file_id):
    """Scan a file's track metadata from the file-operation queue.

    The file half runs here — dropping empty subtitle tracks and parsing —
    and the extracted details are handed to save_track_metadata on the sql
    queue, with the title lock passing along. Retries later if the title
    is locked by another task.
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
                sleep_duration = random.randint(5, 15)
                current_app.logger.warning(
                    f"'{file.basename}' Lock exists, "
                    f"returning to queue after {sleep_duration} minutes"
                )
                current_app.file_queue.enqueue_in(
                    timedelta(minutes=sleep_duration),
                    "app.videos.track_metadata_scan_task",
                    file_id=file_id,
                    job_timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
                    job_id=safe_job_id(f"retry:track_metadata_scan_task:{file_id}"),
                    result_ttl=86400,
                    description=f"'{file.basename}'",
                )
                return True

            try:
                details = extract_track_metadata(file_path)
            except Exception:
                current_app.lock_manager.unlock(lock)
                raise

            current_app.sql_queue.enqueue(
                "app.videos.save_track_metadata",
                args=(file_id, details, lock),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"{file.basename} – Saving track metadata",
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        return True


def record_filesize(file, size_bytes):
    """Store a file's size on its record.

    Every task that rewrites a library file owes its row this: the
    supplement and remux paths change the file's size on disk, and a
    row left holding the old one misreports the library and disagrees
    with the archived copy's size.

    One line, but a named one — it's the call the in-place rewrite
    contract is asserted against, and the single place a size is
    written. The MB and GB columns it used to keep in step were dropped:
    four write sites had to remember three fields, three of them with
    their own copy of the arithmetic, and nothing ever read the derived
    two except one line of one template. Sizes are formatted where
    they're displayed now.
    """

    file.filesize_bytes = size_bytes


def save_track_metadata(file_id, details, lock=None):
    """Write extracted track metadata to the database.

    The sql half of a track rescan: everything here is session work fed by
    the details dict. Releases the passed title lock when done.
    """

    with app.app_context():
        try:
            file = File.query.filter_by(id=file_id).first()
            if file is None:
                return False

            # Clear metadata for existing File record

            file.date_updated = datetime.now(timezone.utc)
            FileAudioTrack.query.filter_by(file_id=file.id).delete()
            FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

            # Set file video track info

            for field, value in details["video"].items():
                setattr(file, field, value)

            record_filesize(file, details["filesize_bytes"])
            current_app.logger.info(f"{file} {file.filesize_bytes} bytes")

            # Set file audio track info

            for i, track in enumerate(details["audio_tracks"]):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                file.audio_track = audio_track
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            # Set file subtitle track info

            for i, track in enumerate(details["subtitle_tracks"]):
                track["file_id"] = file.id
                track["track"] = i + 1
                subtitle_track = FileSubtitleTrack(**track)
                file.subtitle_track = subtitle_track
                current_app.logger.info(
                    f"{file} Adding subtitle track {subtitle_track}"
                )
                db.session.add(subtitle_track)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        else:
            db.session.commit()
            return True

        finally:
            if lock:
                current_app.lock_manager.unlock(lock)
                current_app.logger.info(f"Removed lock {lock}")


def track_metadata_scan(file_id):
    """Rescan a file's metadata on demand.

    Returns False without scanning when another task holds this title's lock
    (e.g. a remux, property edit, or transcode in progress).
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
            os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        )
    except Exception:
        current_app.lock_manager.unlock(lock)
        raise

    return save_track_metadata(file_id, details, lock=lock)


# The languages a track can be set to. Read out of mkvtoolnix's own
# table so the file page can never offer a code the edit would reject,
# and narrowed to the ISO 639-2 entries — that's what MediaInfo reports
# back and what the track records store

FALLBACK_LANGUAGES = (
    ("eng", "English"),
    ("und", "Undetermined"),
    ("zxx", "No linguistic content"),
)


@lru_cache(maxsize=1)
def iso_639_2_languages():
    """Every (code, name) pair mkvpropedit accepts, sorted by name.

    Cached for the life of the process: the table belongs to the
    installed mkvtoolnix, not to any one file.
    """

    try:
        listing = subprocess.run(
            [current_app.config["MKVMERGE_BIN"], "--list-languages"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    except Exception:
        # Without the table the page can still offer the three codes
        # this exists to correct between

        current_app.logger.error(traceback.format_exc())
        return FALLBACK_LANGUAGES

    languages = []
    for line in listing.splitlines():
        # "English language name | ISO 639-3 | ISO 639-2 | ISO 639-1";
        # the header and rule rows fail the 3-character check, as do
        # the 639-3-only languages we can't store

        columns = [column.strip() for column in line.split("|")]
        if len(columns) < 3 or len(columns[2]) != 3:
            continue

        languages.append((columns[2], columns[0]))

    if not languages:
        return FALLBACK_LANGUAGES

    return tuple(sorted(languages, key=lambda language: language[1].lower()))


def resolve_language_code(value):
    """The ISO 639-2 code for whatever was typed in a language box.

    The datalist fills in the bare code, but browsers disagree over
    whether they match on an option's label, so a language's name (or
    the "English (eng)" pairing) is accepted just as readily. Returns
    None when nothing matches, which the caller reports rather than
    guessing at.
    """

    value = (value or "").strip()
    if not value:
        return None

    languages = iso_639_2_languages()
    by_code = {code.lower(): code for code, name in languages}
    by_name = {name.lower(): code for code, name in languages}

    if value.lower() in by_code:
        return by_code[value.lower()]

    if value.lower() in by_name:
        return by_name[value.lower()]

    paired = re.fullmatch(r"(?P<name>.+?)\s*\((?P<code>[A-Za-z]{3})\)", value)
    if paired and paired.group("code").lower() in by_code:
        return by_code[paired.group("code").lower()]

    return None


def mkvpropedit_task(
    file_id,
    default_audio_track,
    default_subtitle_track,
    forced_subtitle_tracks,
    track_languages=None,
    transient_retries=0,
):
    """Update a file's MKV properties.

    `track_languages` maps a track to the ISO 639-2 code it should be
    given, keyed the way mkvpropedit names tracks ({"a1": "eng"});
    None (or an empty mapping) leaves every language alone.
    """

    # Shared lock/retry/copy plumbing stays in app.videos; lazy so the
    # module import direction stays one-way

    from app.videos import (
        MAX_TRANSIENT_RETRIES,
        TRANSIENT_COPY_ERRNOS,
        acquire_lock_or_defer,
    )

    with app.app_context():
        file = File.query.filter_by(id=file_id).first()

        # Serialize with other tasks that rewrite this title's files or
        # track records

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
        )
        if not lock:
            return True

        try:
            return mkvpropedit_unlocked(
                file_id,
                default_audio_track,
                default_subtitle_track,
                forced_subtitle_tracks,
                track_languages,
            )
        except OSError as e:
            if (
                e.errno in TRANSIENT_COPY_ERRNOS
                and transient_retries < MAX_TRANSIENT_RETRIES
                and not getattr(e, "retry_unsafe", False)
            ):
                # A flaky mount interrupted the edit before the file was
                # restructured, so the same track arguments are still valid:
                # retry once the mount settles. The finally releases the
                # lock, and the retry takes it again like any fresh run

                current_app.logger.warning(
                    f"'{file.basename}' MKV property edit failed with a "
                    f"transient I/O error ({e}), retrying in 5 minutes "
                    f"(attempt {transient_retries + 1} of {MAX_TRANSIENT_RETRIES})"
                )
                current_app.file_queue.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.mkvpropedit_task",
                    file_id,
                    default_audio_track,
                    default_subtitle_track,
                    forced_subtitle_tracks,
                    track_languages,
                    transient_retries=transient_retries + 1,
                    job_timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
                    job_id=retry_job_id(
                        "mkvpropedit_task", file_id, transient_retries + 1
                    ),
                    result_ttl=86400,
                    description=f"'{file.basename}'",
                )
                return False
            current_app.logger.error(traceback.format_exc())
            raise
        finally:
            current_app.lock_manager.unlock(lock)


def mkvpropedit_unlocked(
    file_id,
    default_audio_track,
    default_subtitle_track,
    forced_subtitle_tracks,
    track_languages=None,
):
    """Update a file's MKV properties; the caller must hold the title's lock."""

    from app.videos import wait_for_subprocess

    with app.app_context():
        # Once the reorder remux is renamed into place the file's track
        # numbering has changed, so retrying with the caller's original
        # track arguments would flag the wrong tracks

        reordered = False

        try:
            job = get_current_job()

            # Get the record of the file to modify

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

            if job:
                job.meta["description"] = f"'{file.basename}' — Updating MKV properties"
                job.meta["progress"] = -1
                job.save_meta()

            FileAudioTrack.query.filter_by(file_id=file.id).delete()
            FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

            media_info = MediaInfo.parse(file_path)
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

            # The web form sends track ids as strings; mkvmerge_task sends ints,
            # and None means the file has no audio tracks to set a default on.
            # Normalize once so every comparison below compares like with like.

            if default_audio_track is not None:
                default_audio_track = str(default_audio_track)
            if default_subtitle_track is not None:
                default_subtitle_track = str(default_subtitle_track)

            audio_track_arguments = []
            subtitle_track_arguments = []
            language_arguments = []

            for track_id, track in enumerate(audio_tracks, 1):
                track_id = str(track_id)

                if track_id == default_audio_track:
                    audio_track_arguments.append(
                        f"--edit track:a{track_id} --set flag-default=1"
                    )

                else:
                    audio_track_arguments.append(
                        f"--edit track:a{track_id} --set flag-default=0"
                    )

            if default_subtitle_track or forced_subtitle_tracks:
                for track_id, track in enumerate(subtitle_tracks, 1):
                    track_id = str(track_id)

                    if track_id == default_subtitle_track:
                        subtitle_track_arguments.append(
                            f"--edit track:s{track_id} --set flag-default=1"
                        )

                    else:
                        subtitle_track_arguments.append(
                            f"--edit track:s{track_id} --set flag-default=0"
                        )

                    if track_id in forced_subtitle_tracks:
                        subtitle_track_arguments.append(
                            f"--edit track:s{track_id} --set flag-forced=1"
                        )

                    else:
                        subtitle_track_arguments.append(
                            f"--edit track:s{track_id} --set flag-forced=0"
                        )

            # Language corrections stand on their own: a file whose
            # default flags are already right still needs the edit applied,
            # so they're collected outside the flag loops above.
            # mkvpropedit carries the change into LanguageIETF for us

            for track_type, tracks in (("a", audio_tracks), ("s", subtitle_tracks)):
                for track_id, track in enumerate(tracks, 1):
                    language = (track_languages or {}).get(f"{track_type}{track_id}")
                    if language:
                        language_arguments.append(
                            f"--edit track:{track_type}{track_id} "
                            f"--set language={language}"
                        )

            current_app.logger.info(
                f"{file.basename} audio_track_arguments: {audio_track_arguments}"
            )
            current_app.logger.info(
                f"{file.basename} subtitle_track_arguments: {subtitle_track_arguments}"
            )
            current_app.logger.info(
                f"{file.basename} language_arguments: {language_arguments}"
            )

            # subprocess expects an array of arguments,
            # so we need to split the arguments on spaces
            localization_arguments = []
            for arg in audio_track_arguments:
                localization_arguments.extend(arg.split())

            for arg in subtitle_track_arguments:
                localization_arguments.extend(arg.split())

            for arg in language_arguments:
                localization_arguments.extend(arg.split())

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
                for line in mkvpropedit_task.stdout:
                    line = line.replace("\n", "")
                    current_app.logger.info(f"'{file.basename}' {line}")

                wait_for_subprocess(mkvpropedit_task, ok_returncodes=(0, 1))

                # If the default audio track isn't the first track, create a new file with the
                # default audio track prioritized so Plex selects it first

                if default_audio_track is not None and default_audio_track != "1":
                    new_track_order = []
                    media_info = MediaInfo.parse(file_path)

                    # Default video tracks
                    for track in media_info.tracks:
                        if track.track_type == "Video" and track.default == "Yes":
                            new_track_order.append(f"0:{track.streamorder}")

                    # Non-default video tracks
                    for track in media_info.tracks:
                        if track.track_type == "Video" and track.default == "No":
                            new_track_order.append(f"0:{track.streamorder}")

                    # Default audio tracks
                    for track in media_info.tracks:
                        if track.track_type == "Audio" and track.default == "Yes":
                            new_track_order.append(f"0:{track.streamorder}")

                    # Non-default audio tracks

                    for track in media_info.tracks:
                        if track.track_type == "Audio" and track.default == "No":
                            new_track_order.append(f"0:{track.streamorder}")

                    # Default subtitle tracks

                    for track in media_info.tracks:
                        if track.track_type == "Text" and track.default == "Yes":
                            new_track_order.append(f"0:{track.streamorder}")

                    # Non-default subtitle tracks

                    for track in media_info.tracks:
                        if track.track_type == "Text" and track.default == "No":
                            new_track_order.append(f"0:{track.streamorder}")

                    new_track_order = ",".join(new_track_order)

                    current_app.logger.info(
                        f"{file.basename} new_track_order: {new_track_order}"
                    )

                    output_directory = os.path.join(
                        current_app.config["LIBRARY_DIR"], file.dirname
                    )
                    hidden_output_file = os.path.join(
                        output_directory, f".{file.basename}"
                    )

                    command = [
                        current_app.config["MKVMERGE_BIN"],
                        "--track-order",
                        new_track_order,
                        "-o",
                        hidden_output_file,
                        file_path,
                    ]

                    current_app.logger.info(
                        f"'{file.basename}' Running mkvmerge: {command}"
                    )

                    mkvmerge_process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                    )

                    watch_mkvmerge_progress(
                        mkvmerge_process, job, file.basename, "Remuxing"
                    )

                    wait_for_subprocess(mkvmerge_process, ok_returncodes=(0, 1))

                    # Move the new file into place

                    os.rename(hidden_output_file, file_path)
                    reordered = True

            # Remove any subtitle tracks that have zero elements

            remove_empty_subtitle_tracks(file_path)

            # Rebuild the audio and subtitle track info now that we've made modifications

            output_audio_tracks = get_audio_tracks_from_file(file_path)
            output_subtitle_tracks = get_subtitle_tracks_from_file(file_path)

            # Set file audio track info

            for i, track in enumerate(output_audio_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                file.audio_track = audio_track
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            # Set file subtitle track info

            flag_possibly_forced_subtitles(file, output_subtitle_tracks)

            for i, track in enumerate(output_subtitle_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                subtitle_track = FileSubtitleTrack(**track)
                file.subtitle_track = subtitle_track
                current_app.logger.info(
                    f"{file} Adding subtitle track {subtitle_track}"
                )
                db.session.add(subtitle_track)

            record_filesize(file, os.path.getsize(file_path))
            file.date_updated = datetime.now(timezone.utc)

        except OSError as e:
            # Tell the caller whether retrying with the same arguments is
            # still safe; logging (or a quiet transient defer) is its job

            e.retry_unsafe = reordered
            db.session.rollback()
            raise

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        else:
            db.session.commit()

            # The file on disk changed: default flags, and after a
            # reorder its whole track layout — re-read by Plex now
            # rather than whenever its own scan next comes round

            enqueue_plex_analyze(file_path)

        if current_app.config["ARCHIVE_ORIGINAL_MEDIA"]:
            try:
                (
                    file.aws_untouched_key,
                    file.aws_untouched_date_uploaded,
                    file.aws_untouched_filesize_bytes,
                ) = aws_upload(
                    file_path,
                    current_app.config["AWS_UNTOUCHED_PREFIX"],
                    force_upload=True,
                    ignore_etag=True,
                )

            except OSError as e:
                # The edit itself already succeeded and committed, so a
                # whole-task retry could re-edit a restructured file; only
                # the re-upload was lost, and the S3 sync task heals that

                e.retry_unsafe = True
                current_app.logger.error(traceback.format_exc())
                db.session.rollback()
                raise

            except Exception:
                current_app.logger.error(traceback.format_exc())
                db.session.rollback()
                raise

            else:
                db.session.commit()

        return True


def mkvmerge_task(file_id, audio_tracks, subtitle_tracks):
    """Remux a MKV file."""

    from app.videos import acquire_lock_or_defer

    with app.app_context():
        file = File.query.filter_by(id=file_id).first()

        # Serialize with other tasks that rewrite this title's files or
        # track records

        lock = acquire_lock_or_defer(
            file.file_identifier(),
            current_app.config["MKVPROPEDIT_TASK_TIMEOUT"] * 1000,
            current_app.import_queue,
            "app.videos.mkvmerge_task",
            minutes=(5, 15),
            timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            args=(file_id, audio_tracks, subtitle_tracks),
        )
        if not lock:
            return True

        try:
            return mkvmerge_unlocked(file_id, audio_tracks, subtitle_tracks)
        finally:
            current_app.lock_manager.unlock(lock)


def mkvmerge_unlocked(file_id, audio_tracks, subtitle_tracks):
    """Remux a MKV file; the caller must hold the title's lock."""

    from app.videos import wait_for_subprocess

    with app.app_context():
        try:
            job = get_current_job()

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

            FileAudioTrack.query.filter_by(file_id=file.id).delete()
            FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

            output_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file.dirname
            )
            hidden_output_file = os.path.join(output_directory, f".{file.basename}")

            audio_start = None
            subtitle_start = None

            media_info = MediaInfo.parse(file_path)
            tracks = [
                track for track in media_info.tracks if track.track_id is not None
            ]

            current_app.logger.info(
                f"MediaInfo tracks: "
                f"{[(track.track_type, track.track_id, track.streamorder) for track in tracks]}"
            )

            for i, track in enumerate(tracks):
                if track.track_type == "Audio" and audio_start == None:
                    audio_start = i
                if track.track_type == "Text" and subtitle_start == None:
                    subtitle_start = i

            current_app.logger.info(f"Audio tracks: {audio_tracks}")
            current_app.logger.info(f"Subtitle tracks: {subtitle_tracks}")

            current_app.logger.info(f"First audio track: {audio_start}")
            current_app.logger.info(f"First subtitle track: {subtitle_start}")

            audio_tracks = [audio_start + int(track) - 1 for track in audio_tracks]
            subtitle_tracks = [
                subtitle_start + int(track) - 1 for track in subtitle_tracks
            ]

            current_app.logger.info(f"Modified audio tracks: {audio_tracks}")
            current_app.logger.info(f"Modified subtitle tracks: {subtitle_tracks}")

            command = [
                current_app.config["MKVMERGE_BIN"],
                "-o",
                hidden_output_file,
                "--title",
                "",
                "--track-name",
                "-1:",
            ]

            if len(audio_tracks) >= 1:
                output_audio_tracks = ",".join(map(str, audio_tracks))
                command.extend(["-a", output_audio_tracks])
            else:
                command.append("--no-audio")

            if len(subtitle_tracks) >= 1:
                output_subtitle_tracks = ",".join(map(str, subtitle_tracks))
                command.extend(["-s", output_subtitle_tracks])
            else:
                command.append("--no-subtitles")

            command.append(file_path)

            current_app.logger.info(f"'{file.basename}' Running mkvmerge: {command}")

            mkvmerge_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )

            watch_mkvmerge_progress(mkvmerge_process, job, file.basename, "Remuxing")

            wait_for_subprocess(mkvmerge_process, ok_returncodes=(0, 1))

            # Move the new file into place

            os.rename(hidden_output_file, file_path)

            # Remove any subtitle tracks that have zero elements

            remove_empty_subtitle_tracks(file_path)

            # Rebuild the audio and subtitle track info now that we've made modifications

            output_audio_tracks = get_audio_tracks_from_file(file_path)
            output_subtitle_tracks = get_subtitle_tracks_from_file(file_path)

            # Set file audio track info

            for i, track in enumerate(output_audio_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                file.audio_track = audio_track
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            # Set file subtitle track info

            flag_possibly_forced_subtitles(file, output_subtitle_tracks)

            for i, track in enumerate(output_subtitle_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                subtitle_track = FileSubtitleTrack(**track)
                file.subtitle_track = subtitle_track
                current_app.logger.info(
                    f"{file} Adding subtitle track {subtitle_track}"
                )
                db.session.add(subtitle_track)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        else:
            db.session.commit()
            # This task already holds the title's lock, so call the unlocked
            # variant directly instead of deadlocking against ourselves.
            # OSErrors from it leave logging to the caller, so log here

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
                audio_track["language"] = "und"
                audio_track["language_name"] = "Undetermined"

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

            # Change track channel layout to include LFE track if present;
            # leave as None if MediaInfo didn't report a usable channel count
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
                # The 3-character language code is usually in the 4th position in the
                # other_language variable, but sometimes the other_language variable only
                # has 3 elements. If other_language doesn't have a 4th element, default
                # to "Undetermined" / "und", check to see if any values are 3 characters
                # long, and use it if it exists.

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


def flag_possibly_forced_subtitles(file, subtitle_tracks):
    """Speculate which subtitle tracks might actually be forced subtitle tracks.

    If a track has elements, but no more than 1/3 the elements of the first
    subtitle track, and it isn't already marked as forced, mark its forced flag
    as unknown (None) and report that the file may have a forced subtitle track.
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


def remove_empty_subtitle_tracks(file_path):
    """Remux a Matroska file in place to drop subtitle tracks with zero elements.

    Only tracks whose statistics tags explicitly report zero elements are
    removed; a track with no statistics at all is left alone, since we can't
    tell whether it's actually empty.

    Returns True if the file was rewritten, False if there was nothing to remove.
    """

    from app.videos import wait_for_subprocess

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

        # Tracks are selected by their mkvmerge track id (the stream order),
        # so we can't safely remux if any id is unknown

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

    hidden_output_file = os.path.join(
        os.path.dirname(file_path), f".{basename}.remove-empty-subs.mkv"
    )

    command = [
        current_app.config["MKVMERGE_BIN"],
        "-o",
        hidden_output_file,
    ]

    if keep_track_ids:
        command.extend(["-s", ",".join(str(id) for id in keep_track_ids)])
    else:
        command.append("--no-subtitles")

    command.append(file_path)
    current_app.logger.info(f"'{basename}' Running mkvmerge: {command}")

    mkvmerge_process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    watch_mkvmerge_progress(
        mkvmerge_process, job, basename, "Removing empty subtitle tracks"
    )

    wait_for_subprocess(mkvmerge_process, ok_returncodes=(0, 1))

    # Move the new file into place

    os.rename(hidden_output_file, file_path)
    return True


def plan_audio_supplements(audio_tracks):
    """The output audio-track order for the supplement pass, as
    (action, source index) pairs — action "flac" converts that source
    track, "copy" carries it through.

    Every lossless track that isn't already FLAC or PCM gets a FLAC
    twin placed immediately before it, mirroring the MakeMKV "FLAC
    Plus Original Audio" rip profile; the original is always kept.
    A FLAC counts as an existing twin ONLY when it sits immediately
    before a lossless track in the same language — the exact shape
    the rip profile produces. A FLAC anywhere else could be anything
    (a commentary, say), so it is never counted as a twin, never
    moved, and never given the default slot (Glenn's rule); its
    neighbor earns a freshly converted twin instead. Channel counts
    deliberately do NOT have to match: MediaInfo labels DTS-ES Matrix
    sources "6.0" while their discrete content — and therefore any
    lossless FLAC decode of them — is 5.1 (the LOTR discs), so a
    channel-strict match would call correct twins imperfect and stack
    redundant ones. Files already in the twinned shape plan as pure
    copies, keeping the pass idempotent across disc rips and S3
    re-downloads.
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


def build_supplement_args(plan):
    """The ffmpeg audio arguments realizing a supplement plan.

    Codec and disposition options are numbered by OUTPUT position —
    a source track mapped twice (converted twin + original) shifts
    every later output index, so the input index only ever appears in
    the -map selector. The first output track is the default and all
    others are cleared, matching the rip profile's convention that
    the natively playable track leads.
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


def supplement_lossless_tracks(file_path, file_id=None):
    """Give every lossless non-FLAC/PCM audio track a FLAC twin placed
    just before it, keeping the original.

    The twin plays natively on Apple TV clients while the lossless
    original stays for direct play and future passthrough. Files whose
    twins already exist — MakeMKV rips made with the "FLAC Plus
    Original Audio" profile, or re-downloads of already-supplemented
    uploads — plan as pure copies and pass through untouched, so the
    pass is safe to repeat.
    """

    from app.videos import wait_for_subprocess

    with app.app_context():
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

                    # Convert the file duration from milliseconds to seconds
                    file_duration = int(track.duration) / 1000
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
                    "-map",
                    "0:v:0",
                    "-c:v:0",
                    "copy",
                ]
                + audio_args
                + [
                    "-map",
                    "0:s:?",
                    "-c:s",
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
                progress_match = re.search(
                    r"time\=(?P<hour>\d{2})\:(?P<minute>\d{2}):(?P<seconds>\d{2})",
                    line,
                )
                if progress_match and file_duration:
                    hour = int(progress_match.group("hour"))
                    minutes = int(progress_match.group("minute"))
                    seconds = int(progress_match.group("seconds"))
                    progress = int(
                        (((hour * 3600) + (minutes * 60) + seconds) / file_duration)
                        * 100
                    )
                if previous_percent != progress:
                    current_app.logger.info(
                        f"'{basename}' Supplementing lossless tracks with FLAC: {progress}%"
                    )
                    previous_percent = progress
                if job:
                    job.meta["description"] = (
                        f"'{basename}' — Supplementing lossless tracks with FLAC"
                    )
                    job.meta["progress"] = progress
                    job.save_meta()

            wait_for_subprocess(flac_track_process)

            current_app.logger.info(f"'{basename}' Supplemented lossless tracks")
            current_app.logger.info(f"Moving '{temp_flac_file}' to '{file_path}'")
            shutil.move(temp_flac_file, file_path)

            if file_id:
                track_metadata_scan_task(file_id)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            raise

        else:
            return True


def remux_audio_plan_task(file_id, plan):
    """Task: rebuild one LIBRARY file's audio to an explicit supplement
    plan — (action, source index) pairs in plan_audio_supplements'
    format, except hand-built, so a track can be replaced or dropped
    (what the automatic planner never does). Born for the DTS-ES discs' imperfect
    DTS-ES twins: [["flac", 1], ["copy", 1], ["copy", 2]] decodes the
    MA into a fresh 6.0 twin and drops the old 5.1 one.

    Copy-first, the atmos task's posture throughout: one staging copy
    in, remux + verification on local disk, the verified result
    replaces the library copy, the track rows rebuild, and the
    untouched archive is force-replaced.
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
        )
        if not lock:
            return True

        try:
            return _remux_audio_plan_unlocked(int(file_id), plan)
        finally:
            current_app.lock_manager.unlock(lock)


def _remux_audio_plan_unlocked(file_id, plan):
    """The remux pipeline; the caller must hold the title's lock."""

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
        staging_source = os.path.join(staging_dir, f".src-{basename}")
        staging_output = os.path.join(staging_dir, basename)
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
                    file_duration = int(track.duration) / 1000
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
                    "0:v:0",
                    "-c:v:0",
                    "copy",
                ]
                + audio_args
                + ["-map", "0:s:?", "-c:s", "copy", staging_output],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"'{basename}' ffmpeg failed: {result.stderr[-500:]}"
                )

            # Never replace the library copy with a remux that didn't
            # deliver: track count and per-position codecs must match
            # the plan, and the duration must survive

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

            # The output already carries the clean basename (unlike the
            # atmos task, whose OUTPUT was the dotfile) — so the upload
            # derives the right S3 key from staging_output itself; the
            # first run renamed output onto the .src- name and uploaded
            # 40GB under 'untouched/src-…' (the Fellowship incident)

            os.remove(staging_source)
            final_staging = staging_output

            hidden_library = os.path.join(os.path.dirname(file_path), f".{basename}")
            copy_with_progress(
                final_staging, hidden_library, job, basename, "Copying to library"
            )
            os.replace(hidden_library, file_path)

            # Rebuild the track records now that the file changed

            FileAudioTrack.query.filter_by(file_id=file.id).delete()
            FileSubtitleTrack.query.filter_by(file_id=file.id).delete()
            for i, track in enumerate(new_audio_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                db.session.add(FileAudioTrack(**track))
            flag_possibly_forced_subtitles(file, new_subtitle_tracks)
            for i, track in enumerate(new_subtitle_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                db.session.add(FileSubtitleTrack(**track))
            record_filesize(file, os.path.getsize(final_staging))
            file.date_updated = datetime.now(timezone.utc)
            db.session.commit()

            # A rebuilt audio layout, re-read now rather than at
            # Plex's own pace

            enqueue_plex_analyze(file_path)

            if current_app.config["ARCHIVE_ORIGINAL_MEDIA"]:
                try:
                    (
                        file.aws_untouched_key,
                        file.aws_untouched_date_uploaded,
                        file.aws_untouched_filesize_bytes,
                    ) = aws_upload(
                        final_staging,
                        current_app.config["AWS_UNTOUCHED_PREFIX"],
                        force_upload=True,
                        ignore_etag=True,
                    )
                    db.session.commit()
                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    db.session.rollback()
                    raise
            return True

        finally:
            for stray in (staging_source, staging_output):
                try:
                    os.remove(stray)
                except OSError:
                    pass


# This process's app instance, resolved lazily so importing this module from
# a process that already has an application doesn't build a second one

app = LocalProxy(get_app)
