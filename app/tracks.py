"""Read and rewrite the streams inside a media file (the split from app.videos).

This module has the MediaInfo scan and the stored track metadata
(this includes the Dolby Vision profile parse), the mkvpropedit flag
editor, the mkvmerge remuxer with their task wrappers that hold the
locks, the subtitle inspection (possibly-forced flags, removal of
empty tracks), and the planner and remuxers for the lossless-audio
supplements.

app.videos exports each name here again. Thus, the stored rq job
strings ("app.videos.mkvmerge_task") and the import sites continue to
resolve. The shared lock, subprocess, and copy code stays in
app.videos. The functions here import it lazily. Thus, the module
import direction stays one-way.
"""

import json
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
from app.aws_storage import aws_upload, mark_archive_stale
from app.models import File, FileAudioTrack, FileSubtitleTrack
from app.plex_library import enqueue_plex_analyze
from app.smb_probe import library_path, probe_and_record


def watch_mkvmerge_progress(process, job, name, activity):
    """Stream the output of a process. Log its progress and update the job meta."""

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

    return {
        "video": video,
        "audio_tracks": get_audio_tracks_from_file(file_path),
        "subtitle_tracks": get_subtitle_tracks_from_file(file_path),
        "filesize_bytes": os.path.getsize(file_path),
    }


def extract_track_metadata(file_path):
    """Remove the empty subtitle tracks and return the media details of a library file.

    This is the file half of a track scan. The returned details go to
    save_track_metadata on the sql queue.
    """

    media_info = MediaInfo.parse(file_path)
    for track in media_info.tracks:
        if track.track_type == "General" and track.format == "Matroska":
            remove_empty_subtitle_tracks(file_path)
            break

    return _extract_media_details(file_path)


def track_metadata_scan_library():
    """Add all the files in the library to the metadata scan queue."""

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


def save_track_metadata(file_id, details, lock=None):
    """Write the extracted track metadata to the database.

    This is the sql half of a track scan. All work here is session
    work from the details dict. This function releases the given title
    lock when it is done.
    """

    with app.app_context():
        try:
            file = File.query.filter_by(id=file_id).first()
            if file is None:
                return False

            # Clear the metadata of the existing File record

            file.date_updated = datetime.now(timezone.utc)
            FileAudioTrack.query.filter_by(file_id=file.id).delete()
            FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

            # Set the video track info of the file

            for field, value in details["video"].items():
                setattr(file, field, value)

            record_filesize(file, details["filesize_bytes"])
            current_app.logger.info(f"{file} {file.filesize_bytes} bytes")

            # Set the audio track info of the file

            for i, track in enumerate(details["audio_tracks"]):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                file.audio_track = audio_track
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            # Set the subtitle track info of the file

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
    """Scan the metadata of a file again on demand.

    If a different task holds the lock of this title (for example, a
    remux, a property edit, or a transcode in progress), this function
    returns False without a scan.
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
        # The 639-3-only languages also fail it. They have no code that
        # the records can hold

        columns = [column.strip() for column in line.split("|")]
        if len(columns) < 4 or len(columns[2]) != 3:
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
            ((iso_639_2, name) for name, _, iso_639_2, _ in rows),
            key=lambda language: language[1].lower(),
        )
    )


@lru_cache(maxsize=1)
def _language_aliases():
    """Map each name of a language, in lowercase, to its 639-2 code.

    The names are the language name, each code column in the table,
    and the ISO 639-2/T spellings that are not in the table. See
    ISO_639_2_TERMINOLOGIC.
    """

    aliases = {}
    for name, iso_639_3, iso_639_2, iso_639_1 in _language_table():
        for alias in (name, iso_639_3, iso_639_2, iso_639_1):
            if alias:
                aliases.setdefault(alias.lower(), iso_639_2)

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

    names = dict(iso_639_2_languages())
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

    from app.models import FileAudioTrack, FileSubtitleTrack, Movie

    # Three DISTINCT queries over the track tables take approximately
    # 40 ms. That is too much for each File page render. The answer
    # changes only when a new language enters the library

    cached = current_app.redis.get(LANGUAGE_CHOICES_KEY)
    if cached:
        return tuple(tuple(pair) for pair in json.loads(cached))

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
        if iso_639_1
    }
    codes |= {
        "und",
        "zxx",
        resolve_language_code(current_app.config["NATIVE_LANGUAGE"]),
    }
    codes.discard(None)

    names = dict(iso_639_2_languages())
    choices = tuple(
        sorted(
            ((code, names.get(code, code)) for code in codes),
            key=lambda language: language[1].lower(),
        )
    )
    current_app.redis.set(
        LANGUAGE_CHOICES_KEY, json.dumps(choices), ex=LANGUAGE_CHOICES_SECONDS
    )
    return choices


def resolve_language_code(value):
    """Return the ISO 639-2 code for the text typed in a language box.

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
        else:
            probe_and_record(probed_path, context="mkvpropedit_task")
            return edited
        finally:
            current_app.lock_manager.unlock(lock)


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
        # After the reorder remux is renamed into place, the track
        # numbering of the file is different. Thus, a retry with the
        # original track arguments of the caller would flag the wrong
        # tracks

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

            # The web form sends the track ids as strings. mkvmerge_task
            # sends ints. None means that the file has no audio tracks for a
            # default. Normalize them 1 time. Thus, each comparison below
            # compares the same types.

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

            # The language corrections are independent. A file whose default
            # flags are already correct still needs the edit. Thus, Fitzflix
            # collects them outside the flag loops above. mkvpropedit also
            # applies the change to LanguageIETF

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

            # subprocess expects an array of arguments. Thus, split the
            # arguments on the spaces
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

                # If the default audio track is not the first track, make a
                # new file with the default audio track first. Then Plex
                # selects it first

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

            # Remove the subtitle tracks that have zero elements

            remove_empty_subtitle_tracks(file_path)

            # Build the audio and subtitle track info again after the changes

            output_audio_tracks = get_audio_tracks_from_file(file_path)
            output_subtitle_tracks = get_subtitle_tracks_from_file(file_path)

            # Set the audio track info of the file

            for i, track in enumerate(output_audio_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                file.audio_track = audio_track
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            # Set the subtitle track info of the file

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
            # Tell the caller if a retry with the same arguments is still
            # safe. The log entry (or a quiet transient defer) is the job of
            # the caller

            e.retry_unsafe = reordered
            db.session.rollback()
            raise

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        else:
            db.session.commit()

            # The file on disk changed. The default flags changed. After a
            # reorder, the whole track layout changed. Plex reads it again
            # now, not at its next scan

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
                file.aws_untouched_stale = False

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
        )
        if not lock:
            return True

        try:
            return mkvmerge_unlocked(file_id, audio_tracks, subtitle_tracks)
        finally:
            current_app.lock_manager.unlock(lock)


def mkvmerge_unlocked(file_id, audio_tracks, subtitle_tracks):
    """Remux an MKV file. The caller must hold the lock of the title."""

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

            # Remove the subtitle tracks that have zero elements

            remove_empty_subtitle_tracks(file_path)

            # Build the audio and subtitle track info again after the changes

            output_audio_tracks = get_audio_tracks_from_file(file_path)
            output_subtitle_tracks = get_subtitle_tracks_from_file(file_path)

            # Set the audio track info of the file

            for i, track in enumerate(output_audio_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                file.audio_track = audio_track
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            # Set the subtitle track info of the file

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


def flag_possibly_forced_subtitles(file, subtitle_tracks):
    """Find the subtitle tracks that can be forced subtitle tracks.

    A track qualifies if it has elements, but not more than 1/3 of the
    elements of the first subtitle track, and it is not marked as
    forced. This function sets the forced flag of that track to unknown
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


def remove_empty_subtitle_tracks(file_path):
    """Remux a Matroska file in place to remove the subtitle tracks with zero elements.

    This function removes only the tracks whose statistics tags report
    zero elements. It does not change a track with no statistics.
    Fitzflix cannot know if that track is empty.

    Return True if the function rewrote the file. Return False if there
    was nothing to remove.
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
    built again. The untouched archive is replaced by force.
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

            # The output already has the clean basename. In the atmos task,
            # the OUTPUT was the dotfile. Thus, the upload derives the
            # correct S3 key from staging_output. The first run renamed the
            # output to the .src- name and uploaded 40 GB under
            # 'untouched/src-…' (the Fellowship incident)

            os.remove(staging_source)
            final_staging = staging_output

            hidden_library = os.path.join(os.path.dirname(file_path), f".{basename}")
            copy_with_progress(
                final_staging, hidden_library, job, basename, "Copying to library"
            )
            os.replace(hidden_library, file_path)

            # Build the track records again because the file changed

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

            # The audio layout is new. Plex reads it again now, not at its
            # own pace

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


# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, an import of this module from a process that already has an
# application does not build a second one

app = LocalProxy(get_app)
