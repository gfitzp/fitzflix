"""Convert HDR and wide-color imports to 1080p SDR (#292).

The Plex library holds only Rec.709 SDR files. These play on the least
capable equipment of the Fitzflix users. An import with HDR10, HDR10+,
HLG, Dolby Vision, or BT.2020 colors is archived to S3 untouched. Then
it is converted to 1080p SDR, and only the conversion goes into the
library. The CONVERT_HDR_TO_SDR setting turns this off. Then an HDR
file goes into the library as it is.

The flow:

- localization_task calls plan_conversion. A plan renames the import
  to its 1080p quality, for example Bluray-2160p to Bluray-1080p.
- If the library has a real rip of the title at that quality or
  better, the original waits in HELD_DIR for an admin decision
  (hold_import). The choices are "Import", "Archive only", and
  "Discard".
- After the localization, convert_localized_file makes the SDR video on
  the transcode queue. It muxes that video with every other track of
  the localized file. Then the usual move and finalize steps follow.

An ArchivedOriginal row keeps an original in S3 that has no library
file. The S3 sync counts its key as claimed.

app.videos re-exports the task names. Thus, the rq job strings resolve.
"""

import json
import os
import re
import shutil
import subprocess
import time
import traceback

from datetime import datetime, timezone

from pymediainfo import MediaInfo
from rq import get_current_job

from flask import current_app
from werkzeug.local import LocalProxy

from app.tasks import enqueue_task, report_progress
from app import db, get_app, makedirs_on_mounted_volume
from app.models import MOVIES
from app.email import notify_admin

app = LocalProxy(get_app)

# The transfer functions of HDR video, as MediaInfo names them.

HDR_TRANSFERS = ("PQ", "HLG", "SMPTE ST 2084", "ARIB STD-B67")

# The color primaries that a Rec.709 display cannot show without a
# gamut conversion. BT.601 (DVD) is not in the list. It is SDR.

WIDE_PRIMARIES = ("BT.2020",)

# The title lock lives for 1 LOCALIZATION_TASK_TIMEOUT. A conversion can
# take more hours than that. Refresh the lock at this interval.

LOCK_REFRESH_SECONDS = 3600

# The converted file must play for the same time as its source. A
# larger difference means that the encode stopped early.

MAX_DURATION_DRIFT_SECONDS = 2

HANDBRAKE_PROGRESS_RE = re.compile(r"Encoding: task \d+ of \d+, (?P<percent>\d+)")


def needs_conversion(hdr_format, color_primaries, transfer_characteristics):
    """Return True if a video with these color tags is not Rec.709 SDR."""

    return bool(
        hdr_format
        or color_primaries in WIDE_PRIMARIES
        or transfer_characteristics in HDR_TRANSFERS
    )


def describe_color(hdr_format, color_primaries, transfer_characteristics):
    """Return a short name for the color format of a video, for example "HDR10"."""

    from app.tracks import parse_dolby_vision_profile

    profile = parse_dolby_vision_profile(hdr_format)
    if profile:
        return f"Dolby Vision Profile {profile}"
    if hdr_format and "HDR10+" in hdr_format:
        return "HDR10+"
    if transfer_characteristics in ("HLG", "ARIB STD-B67"):
        return "HLG"
    if hdr_format or transfer_characteristics in HDR_TRANSFERS:
        return "HDR10"
    if color_primaries in WIDE_PRIMARIES:
        return "BT.2020 SDR"
    return "SDR"


def probe_video(file_path):
    """Return the color tags, the size, and the duration of the first video track."""

    media_info = MediaInfo.parse(file_path)
    for track in media_info.tracks:
        if track.track_type != "Video":
            continue
        hdr_format = None
        if track.other_hdr_format and track.other_hdr_format[0]:
            hdr_format = track.other_hdr_format[0]
        return {
            "hdr_format": hdr_format,
            "color_primaries": track.color_primaries,
            "transfer_characteristics": track.transfer_characteristics,
            "width": track.width,
            "height": track.height,
            "duration": float(track.duration) / 1000 if track.duration else None,
            "language": track.language,
        }
    return None


def converted_quality_title(quality_title):
    """Return the quality of the 1080p conversion of a source quality.

    The resolution goes down to 1080p. "Remux" goes away, because the
    conversion is an encode. A source below 1080p keeps its quality."""

    title = quality_title.replace(" Remux", "").replace("Remux-", "Bluray-")
    return title.replace("2160p", "1080p")


def plan_conversion(file_path, file_details):
    """Return the conversion plan of an import, or None if it needs no conversion.

    The plan has the source and target qualities, the color format of
    the source, and its Dolby Vision profile. Raise ValueError if the
    target quality does not exist."""

    from app.models import RefQuality
    from app.tracks import parse_dolby_vision_profile

    video = probe_video(file_path)
    if not video or not needs_conversion(
        video["hdr_format"],
        video["color_primaries"],
        video["transfer_characteristics"],
    ):
        return None

    source = file_details["quality_title"]
    target = converted_quality_title(source)
    if not RefQuality.query.filter_by(quality_title=target).first():
        raise ValueError(
            f"There is no quality '{target}' for a conversion of '{source}'"
        )

    return {
        "source_quality": source,
        "target_quality": target,
        "source_format": describe_color(
            video["hdr_format"],
            video["color_primaries"],
            video["transfer_characteristics"],
        ),
        "dolby_vision_profile": parse_dolby_vision_profile(video["hdr_format"]),
        "video_language": video["language"],
    }


def needs_placebo(plan):
    """Return True if the conversion needs the libplacebo ffmpeg (Dolby Vision Profile 5).

    Profile 5 has no HDR10 base layer. HandBrake ignores the Dolby
    Vision reshaping and gets the colors wrong."""

    return bool(plan.get("dolby_vision_profile", "")) and plan[
        "dolby_vision_profile"
    ].startswith("5")


def converted_file_details(file_details, plan):
    """Return a copy of the file details, renamed to the quality of the conversion."""

    details = dict(file_details)
    old_tag = f"[{plan['source_quality']}]"
    new_tag = f"[{plan['target_quality']}]"
    details["basename"] = details["basename"].replace(old_tag, new_tag)
    details["file_path"] = details["file_path"].replace(old_tag, new_tag)
    details["quality_title"] = plan["target_quality"]
    details["converted_from_quality"] = plan["source_quality"]
    details["converted_from_format"] = plan["source_format"]
    return details


def held_decision(file_path):
    """Return the HeldImport row of a held file that an admin chose to import, if any."""

    from app.models import HeldImport

    return HeldImport.query.filter_by(held_path=file_path, decision="import").first()


def hold_import(file_path, file_details, plan, blockers):
    """Move a conversion candidate to HELD_DIR and record it for a decision.

    Return the HeldImport row."""

    from app.models import HeldImport
    from app.pipeline import record_task_stage

    # HELD_DIR is on the external SSD. If the SSD is not mounted, this
    # raises, and the file stays in place (#341)

    held_dir = current_app.config["HELD_DIR"]
    makedirs_on_mounted_volume(held_dir)
    basename = os.path.basename(file_path)
    held_path = os.path.join(held_dir, basename)
    if os.path.abspath(file_path) != os.path.abspath(held_path):
        shutil.move(file_path, held_path)

    held = HeldImport.query.filter_by(held_path=held_path).first()
    if held is None:
        held = HeldImport(held_path=held_path)
        db.session.add(held)
    held.untouched_basename = basename
    held.file_details = json.dumps(file_details)
    held.source_format = plan["source_format"]
    held.filesize_bytes = os.path.getsize(held_path)
    held.existing_file_id = blockers[0].id if blockers else None
    held.decision = None
    held.date_decided = None
    held.date_held = datetime.now(timezone.utc)
    db.session.commit()

    record_task_stage("Waiting for a decision", "done")
    current_app.logger.info(
        f"'{basename}' A real rip of this title is in the library "
        f"({', '.join(f.basename for f in blockers)}). The original waits "
        f"in '{held_dir}' for a decision"
    )
    notify_admin(
        "A conversion waits for a decision",
        "held_import",
        basename=basename,
        source_format=plan["source_format"],
        blockers=blockers,
    )
    return held


def _remove_quietly(*paths):
    """Remove each file that exists. Ignore the errors."""

    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def handbrake_command(input_file, output_file):
    """Return the HandBrake command that makes the SDR video track only."""

    config = current_app.config
    preset_file = (
        ["--preset-import-file", config["HANDBRAKE_PRESET_FILE"]]
        if config["HANDBRAKE_PRESET_FILE"]
        else []
    )
    return (
        [config["HANDBRAKE_BIN"]]
        + preset_file
        + [
            "--preset",
            config["CONVERSION_PRESET"],
            "-i",
            input_file,
            "-o",
            output_file,
            "--audio",
            "none",
            "--subtitle",
            "none",
            "--no-markers",
            "--crop-mode",
            "none",
            "--no-hdr-dynamic-metadata",
        ]
    )


def placebo_command(input_file, output_file):
    """Return the ffmpeg command that makes the SDR video of a Dolby Vision Profile 5 file.

    libplacebo applies the Dolby Vision reshaping, then tone-maps to
    Rec.709. The encode settings are those of the HandBrake preset:
    x264, RF 15, the medium preset."""

    return [
        current_app.config["FFMPEG_PLACEBO_BIN"],
        "-hide_banner",
        "-nostdin",
        "-y",
        "-init_hw_device",
        "vulkan",
        "-i",
        input_file,
        "-map",
        "0:v:0",
        "-vf",
        "libplacebo=w='min(iw,1920)':h=-2:apply_dolbyvision=1:tonemapping=bt.2390"
        ":colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
        ":format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "15",
        "-an",
        "-sn",
        "-dn",
        "-progress",
        "pipe:1",
        output_file,
    ]


def mux_command(video_file, localized_file, output_file, language=None):
    """Return the mkvmerge command that joins the SDR video with the other tracks.

    The video comes from the conversion. The audio, the subtitles, the
    chapters, the attachments, and the tags come from the localized
    file, unchanged."""

    command = [
        current_app.config["MKVMERGE_BIN"],
        "-o",
        output_file,
        "--title",
        "",
    ]
    if language:
        command += ["--language", f"0:{language}"]
    command += [
        "--no-audio",
        "--no-subtitles",
        "--no-buttons",
        "--no-attachments",
        "--no-chapters",
        "--no-global-tags",
        "--no-track-tags",
        video_file,
        "--no-video",
        localized_file,
    ]
    return command


class TitleLockLost(Exception):
    """Another task took the title lock during a conversion (#339)."""


def _run_encode(command, job, basename, lock, total_seconds):
    """Run an encode with progress on the job, and keep the title lock alive.

    If another task took the title lock, the encode stops, and this
    raises TitleLockLost."""

    from app.importing import _hold_title_lock
    from app.videos import wait_for_subprocess

    current_app.logger.info(f"'{basename}' {command}")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    last_refresh = time.monotonic()
    previous_percent = None
    for line in process.stdout:
        percent = None
        match = HANDBRAKE_PROGRESS_RE.search(line)
        if match:
            percent = int(match.group("percent"))
        elif line.startswith("out_time_us=") and total_seconds:
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
                percent = min(99, int(done * 100 / total_seconds))
            except ValueError:
                pass
        if percent is not None and percent != previous_percent:
            previous_percent = percent
            if percent % 5 == 0:
                current_app.logger.info(f"'{basename}' Converting to SDR: {percent}%")
            report_progress(job, basename, "Converting to SDR", percent)
        if time.monotonic() - last_refresh > LOCK_REFRESH_SECONDS:
            if not _hold_title_lock(lock, basename):
                process.kill()
                process.wait()
                raise TitleLockLost(basename)
            last_refresh = time.monotonic()
    wait_for_subprocess(process)


def convert_localized_file(
    source_path, file_details, lock, hidden_output_file, plan, lock_waits=0
):
    """Replace the video of a localized file with a 1080p SDR conversion.

    This runs on the transcode queue, between the localization and the
    move into the library. The source of the encode is the localized
    file. Thus, the audio and subtitle choices of the localization
    apply. After the conversion, the task gives the file to
    move_localized_file. The title lock passes through.

    A failure rejects the import and releases the lock. The original is
    still in the import directory, and its archive is in S3.

    If another task holds the title lock, the conversion waits and tries
    again later (#339). It does not convert without the lock."""

    from app.importing import (
        _hold_title_lock,
        abandon_handoff,
        abandon_import,
        defer_for_title_lock,
        set_staging_reservation,
    )
    from app.videos import wait_for_subprocess

    with app.app_context():
        job = get_current_job()
        basename = file_details.get("basename")
        source_name = file_details.get("untouched_basename") or os.path.basename(
            source_path
        )
        # The work files take the name of the localized file. That name is
        # unique on staging. The basename of an extra is not.

        video_only = f"{hidden_output_file}.sdr-video.mkv"
        muxed = f"{hidden_output_file}.sdr.mkv"

        def defer():
            defer_for_title_lock(
                "app.videos.convert_localized_file",
                (source_path, file_details, lock, hidden_output_file, plan),
                {"lock_waits": lock_waits},
                source_path,
                basename,
            )
            return False

        if not _hold_title_lock(lock, basename):
            return defer()

        try:
            source_video = probe_video(hidden_output_file) or {}
            total_seconds = source_video.get("duration")

            current_app.logger.info(
                f"'{basename}' Converting {plan['source_format']} "
                f"{plan['source_quality']} to {plan['target_quality']} SDR"
            )
            if needs_placebo(plan):
                command = placebo_command(hidden_output_file, video_only)
            else:
                command = handbrake_command(hidden_output_file, video_only)
            _run_encode(command, job, basename, lock, total_seconds)

            report_progress(job, basename, "Joining the SDR video")
            mux_process = subprocess.Popen(
                mux_command(
                    video_only, hidden_output_file, muxed, plan.get("video_language")
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )
            for line in mux_process.stdout:
                current_app.logger.debug(f"'{basename}' {line.rstrip()}")
            wait_for_subprocess(mux_process, ok_returncodes=(0, 1))

            # Make sure that the result is SDR and complete.

            result = probe_video(muxed) or {}
            if needs_conversion(
                result.get("hdr_format"),
                result.get("color_primaries"),
                result.get("transfer_characteristics"),
            ):
                raise RuntimeError(
                    f"The conversion is not SDR: {result.get('hdr_format')}, "
                    f"{result.get('color_primaries')}, "
                    f"{result.get('transfer_characteristics')}"
                )
            if (result.get("height") or 0) > 1080 or (result.get("width") or 0) > 1920:
                raise RuntimeError(
                    f"The conversion is {result.get('width')}x{result.get('height')}"
                )
            if total_seconds and result.get("duration") is not None:
                drift = abs(result["duration"] - total_seconds)
                if drift > MAX_DURATION_DRIFT_SECONDS:
                    raise RuntimeError(
                        f"The conversion plays for {result['duration']:.1f} s. "
                        f"The source plays for {total_seconds:.1f} s"
                    )

            os.replace(muxed, hidden_output_file)
            _remove_quietly(video_only)

            # Only the converted file stays on staging now (#294).

            set_staging_reservation(
                source_name,
                os.path.getsize(hidden_output_file),
                [os.path.basename(hidden_output_file).lstrip(".")],
            )

        except TitleLockLost:
            # The localized file stays on staging for the retry. Only the
            # work files of this encode go.

            _remove_quietly(video_only, muxed)
            return defer()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            abandon_import(
                source_path,
                lock,
                strays=(video_only, muxed, hidden_output_file),
                reservation=source_name,
                reason="conversion failed",
            )
            return False

        current_app.logger.info(
            f"'{basename}' Converted to {result.get('width')}x{result.get('height')} SDR"
        )
        # A failed enqueue must not keep the title lock for its full TTL
        # (#340). The source stays in the import directory.

        try:
            enqueue_task(
                "app.videos.move_localized_file",
                source_path,
                file_details,
                lock,
                hidden_output_file,
                description=f"'{basename}'",
            )
        except Exception:
            abandon_handoff(source_path, lock, hidden_output_file)
            return False
        return True


def archived_original_from_file(file, reason):
    """Return an ArchivedOriginal row for the archive of a File row.

    The caller adds it to the session and commits. The row keeps the
    original in S3 after its library file goes away."""

    from app.models import ArchivedOriginal

    existing = ArchivedOriginal.query.filter_by(aws_key=file.aws_key).first()
    archived = existing or ArchivedOriginal(aws_key=file.aws_key)
    archived.media_library = file.media_library
    archived.movie_id = file.movie_id
    archived.series_id = file.series_id
    archived.season = file.season
    archived.episode = file.episode
    archived.plex_title = file.plex_title
    archived.edition = file.edition if file.media_library == MOVIES else None
    archived.quality_title = file.converted_from_quality or file.quality.quality_title
    archived.hdr_format = file.converted_from_format or file.hdr_format
    archived.untouched_basename = file.untouched_basename or os.path.basename(
        file.aws_key
    )
    archived.aws_filesize_bytes = file.aws_filesize_bytes
    archived.aws_date_uploaded = file.aws_date_uploaded
    archived.reason = reason
    return archived


def archive_held_task(held_id):
    """Archive a held original to S3 and record it as an archive-only original.

    This is the "Archive only" choice. The library does not change. The
    held file is deleted after the upload."""

    from app.aws_storage import aws_upload
    from app.models import ArchivedOriginal, HeldImport, Movie, TVSeries

    with app.app_context():
        held = db.session.get(HeldImport, held_id)
        if held is None:
            return False
        details = held.details()
        try:
            key, uploaded, size = aws_upload(
                held.held_path, current_app.config["AWS_UNTOUCHED_PREFIX"]
            )
            archived = ArchivedOriginal.query.filter_by(aws_key=key).first()
            if archived is None:
                archived = ArchivedOriginal(aws_key=key)
            archived.media_library = details["media_library"]
            if details["media_library"] == MOVIES:
                movie = Movie.query.filter_by(
                    title=details["title"], year=details["year"]
                ).first()
                archived.movie_id = movie.id if movie else None
                archived.edition = details.get("edition")
            else:
                series = TVSeries.query.filter_by(title=details["title"]).first()
                archived.series_id = series.id if series else None
                archived.season = details.get("season")
                archived.episode = details.get("episode")
            archived.plex_title = details.get("plex_title")
            archived.quality_title = details.get(
                "converted_from_quality"
            ) or details.get("quality_title")
            archived.hdr_format = held.source_format
            archived.untouched_basename = held.untouched_basename
            archived.aws_filesize_bytes = size
            archived.aws_date_uploaded = uploaded
            archived.reason = "archive only"
            db.session.add(archived)
            held_path = held.held_path
            db.session.delete(held)
            db.session.commit()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            held = db.session.get(HeldImport, held_id)
            if held:
                held.decision = None
                held.date_decided = None
                db.session.commit()
            return False

        _remove_quietly(held_path)
        current_app.logger.info(
            f"'{archived.untouched_basename}' Archived to S3 as '{key}'. "
            f"The library did not change"
        )
        return True


def prune_held_imports():
    """Remove the HeldImport rows whose held file is gone, for example after a reject."""

    from app.models import HeldImport
    from app.videos import _dead_volumes

    # A dropped volume makes each held file look gone. The prune then
    # deleted each row, and the files in held/ had no record when the
    # volume came back (#319). Thus, a dead or missing held directory
    # prunes nothing. A row is gone only when its folder is there and
    # its file is not.

    held_dir = current_app.config["HELD_DIR"]
    if _dead_volumes([held_dir]) or not os.path.isdir(held_dir):
        current_app.logger.warning(
            f"'{held_dir}' is not available. The held imports stay as they are"
        )
        return 0
    gone = [
        held
        for held in HeldImport.query.all()
        if os.path.isdir(os.path.dirname(held.held_path))
        and not os.path.exists(held.held_path)
    ]
    for held in gone:
        db.session.delete(held)
    if gone:
        db.session.commit()
    return len(gone)
