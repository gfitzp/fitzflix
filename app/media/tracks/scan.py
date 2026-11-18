"""The scan of the tracks of a file with MediaInfo, and the track records."""

import os
import re
import traceback
from datetime import datetime, timezone
from pymediainfo import MediaInfo
from rq import get_current_job
from flask import current_app
from werkzeug.local import LocalProxy
from app.tasks import bulk_job_id, enqueue_task, requeue
from app import db, get_app
from app.archive.restores import refuse_while_restore_pending
from app.models import File, FileAudioTrack, FileSubtitleTrack
from app.settings import get_setting
from app.media.tracks import edits, rewrite_guard
from app.media.importing import chain

app = LocalProxy(get_app)


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


def extract_track_metadata(file_path, file_id=None, rewrite=True):
    """Remove the empty subtitle tracks and return the media details of a library file.

    This is the file half of a track scan. The returned details go to
    save_track_metadata on the sql queue. With file_id, confirm_rewrite
    runs before the remux replaces the file (#517, #726).
    With rewrite False, the file does not change. A scan during a
    pending restore only reads the file (#610).
    """

    media_info = MediaInfo.parse(file_path)
    rewritten = False
    for track in media_info.tracks:
        if not rewrite:
            break
        if track.track_type == "General" and track.format == "Matroska":
            rewritten = edits.remove_empty_subtitle_tracks(
                file_path,
                before_replace=(
                    (lambda: rewrite_guard.confirm_rewrite(file_id, file_path))
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
                    "app.media.tracks.scan.track_metadata_scan_task",
                    file.id,
                    description=f"{file.basename} – Scanning track metadata",
                    job_id=bulk_job_id(TRACK_SCAN_FILE_RUN),
                )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            raise

        return True


def _may_rewrite(file):
    """Return False if a scan must not rewrite the file of a pending restore (#610).

    The caller holds the title lock. The scan still reads the file and
    saves the track rows."""

    refused = refuse_while_restore_pending(file)
    if refused:
        current_app.logger.info(
            f"'{file.basename}' {refused} The scan does not remove the empty "
            f"subtitle tracks"
        )
    return not refused


def track_metadata_scan_task(file_id):
    """Scan the track metadata of a file from the file-operation queue.

    The file half runs here. It removes the empty subtitle tracks and
    parses the file. The extracted details go to save_track_metadata
    on the sql queue, together with the title lock. If a different
    task holds the title lock, this task tries again later.
    """

    with app.app_context():
        # The save job releases the lock after it gets it. A failure
        # before that hand-off releases the lock here (#585).

        lock = None
        handed_off = False
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
                # A job of a library scan keeps the id prefix of the run.
                # Thus, the scan button sees the deferred files (#585).

                job = get_current_job()
                in_run = job is not None and job.id.startswith(TRACK_SCAN_FILE_RUN)
                requeue(
                    "app.media.tracks.scan.track_metadata_scan_task",
                    key=int(file_id),
                    delay=(5, 15),
                    reason="Lock exists",
                    kwargs={"file_id": file_id},
                    description=f"'{file.basename}'",
                    job_id=bulk_job_id(TRACK_SCAN_FILE_RUN) if in_run else None,
                )
                return True

            # Invariant: a scan stores only the file of its record (#750).
            # The import reads a record with the path and the size of a
            # file as its own result (already_cataloged). A scan that
            # stored the size of an upgrade from Radarr or Sonarr hid
            # that file from its import for good.

            if rewrite_guard.replaced_from_outside(
                file.filesize_bytes, file.aws_stale, file_path
            ):

                current_app.lock_manager.unlock(lock)
                lock = None
                current_app.logger.warning(
                    f"'{file.basename}' The library file has an other size than "
                    f"its record. Fitzflix did not write it. The scan stores "
                    f"nothing, and the file goes to the import queue"
                )
                chain.enqueue_localization(file_path)
                return True

            details = extract_track_metadata(
                file_path, file_id, rewrite=_may_rewrite(file)
            )

            # The scan removed empty subtitle tracks. Thus, the library file
            # changed, and its archive must follow (#299). The save queues
            # the upload after its commit. Both write the same row. An
            # upload that starts first fails its commit with MariaDB error
            # 1020 (Oktapodi, 2026-10-24).

            archive = bool(details.pop("rewritten", False)) and get_setting(
                "ARCHIVE_ORIGINAL_MEDIA"
            )

            enqueue_task(
                "app.media.tracks.scan.save_track_metadata",
                file_id,
                details,
                lock,
                archive,
                description=f"{file.basename} – Saving track metadata",
            )
            handed_off = True

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            if lock and not handed_off:
                try:
                    current_app.lock_manager.unlock(lock)
                except Exception:
                    current_app.logger.warning(traceback.format_exc())
            raise

        return True


def save_track_metadata(file_id, details, lock=None, archive=False):
    """Write the extracted track metadata to the database.

    This is the sql half of a track scan. All work here is session
    work from the details dict. This function releases the given title
    lock when it is done. If archive is True, it queues the upload of
    the rewritten library file after its commit. The upload takes the
    title lock. Thus, it is queued after the release of that lock.
    """

    with app.app_context():
        try:
            file = File.query.filter_by(id=file_id).first()
            if file is None:
                return False
            basename = file.basename

            # Set the video track info of the file

            file.date_updated = datetime.now(timezone.utc)
            for field, value in details["video"].items():
                setattr(file, field, value)

            rewrite_guard.record_filesize(file, details["filesize_bytes"])
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

        finally:
            if lock:
                current_app.lock_manager.unlock(lock)
                current_app.logger.info(f"Removed lock {lock}")

        if archive:
            enqueue_task(
                "app.archive.uploads.upload_task",
                file_id,
                description=f"'{basename}'",
            )
        return True


def track_uid(data):
    """Return the Matroska track UID of a MediaInfo track as text, or None (#611).

    data is the to_data() dict of the track. MediaInfo gives the UID as
    unique_id. mkvmerge -J gives the same number as properties.uid. A
    container with no track UIDs gives None."""

    value = str(data.get("unique_id") or "").split(" ", 1)[0]
    return value if value.isdigit() else None


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
            audio_track["uid"] = track_uid(track.to_data())
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
            subtitle_track["uid"] = track_uid(track.to_data())
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
