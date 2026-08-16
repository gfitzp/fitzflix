"""TrueHD Atmos → E-AC-3 Atmos supplement pipeline (#55b).

Apple TV clients can't passthrough lossless TrueHD, so a disc's Atmos
mix plays without its spatial layer. Each TrueHD Atmos track therefore
earns an E-AC-3 JOC (DD+ Atmos) twin that Infuse passes through with
Atmos intact. ffmpeg has no JOC encoder, so the twin comes from AWS
MediaConvert: mkvextract pulls the TrueHD bitstream, truehdd decodes
it to a DAMF Atmos master, MediaConvert encodes EAC3_ATMOS 9.1.6 at
the encoder's 1024k ceiling, and mkvmerge inserts the .ec3 ahead of
the FLAC twin so the trio reads [E-AC-3 Atmos, FLAC, TrueHD Atmos].

Validated end-to-end on Ghost in the Shell (1995): all 15 dynamic
objects + LFE bed intact, exact duration match. MediaConvert bills by
the minute (about $0.85 per feature film) and the pass is idempotent —
a file whose twins exist plans nothing — so re-imports never pay
twice. DAMF intermediates run ~8 GB per film and are cleaned from both
staging and S3 whether the job succeeds or fails.
"""

import glob
import os
import shutil
import subprocess
import time
import traceback

import boto3
from flask import current_app
from pymediainfo import MediaInfo
from rq import get_current_job
from rq.registry import StartedJobRegistry

from app import db, get_app, safe_job_id
from app.models import File, FileAudioTrack, FileSubtitleTrack

app = get_app()

# MediaInfo commercial names, as stored in file_audio_track.codec

TRUEHD_ATMOS_CODEC = "Dolby TrueHD with Dolby Atmos"
EAC3_ATMOS_CODEC = "Dolby Digital Plus with Dolby Atmos"

MEDIACONVERT_POLL_SECONDS = 20
MEDIACONVERT_POLL_LIMIT = 1080  # six hours

# Local scratch requirement: the extracted TrueHD bitstream plus its
# decoded DAMF master, with headroom

WORKSPACE_HEADROOM_BYTES = 16 * 1024**3


def atmos_supplement_candidates(audio_tracks):
    """Indices of TrueHD Atmos tracks still lacking an E-AC-3 Atmos
    twin, matched count-wise per language like the FLAC presence rule —
    so already-supplemented files (and S3 re-downloads of them) plan
    nothing and the pass is idempotent."""

    twins = {}
    for track in audio_tracks:
        if (track.get("codec") or "") == EAC3_ATMOS_CODEC:
            language = track.get("language")
            twins[language] = twins.get(language, 0) + 1

    wanting = []
    for index, track in enumerate(audio_tracks):
        if (track.get("codec") or "") == TRUEHD_ATMOS_CODEC:
            language = track.get("language")
            if twins.get(language, 0) > 0:
                twins[language] -= 1
            else:
                wanting.append(index)
    return wanting


def insertion_point(audio_tracks, source_index):
    """The audio-list position a source's E-AC-3 twin lands at: ahead
    of the FLAC twin when one directly precedes the source (the rip
    profile's pair shape), otherwise directly ahead of the source."""

    if source_index > 0:
        previous = audio_tracks[source_index - 1]
        if previous.get("format") == "FLAC" and previous.get(
            "language"
        ) == audio_tracks[source_index].get("language"):
            return source_index - 1
    return source_index


def mediaconvert_job_settings(input_uri, destination_uri, bitrate):
    """The validated MediaConvert job shape: one DAMF Atmos master in,
    one raw .ec3 out, coded 9.1.6."""

    return {
        "Inputs": [
            {
                "AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
                "FileInput": input_uri,
            }
        ],
        "OutputGroups": [
            {
                "Name": "atmos-ec3",
                "OutputGroupSettings": {
                    "Type": "FILE_GROUP_SETTINGS",
                    "FileGroupSettings": {"Destination": destination_uri},
                },
                "Outputs": [
                    {
                        "ContainerSettings": {"Container": "RAW"},
                        "AudioDescriptions": [
                            {
                                "AudioSourceName": "Audio Selector 1",
                                "CodecSettings": {
                                    "Codec": "EAC3_ATMOS",
                                    "Eac3AtmosSettings": {
                                        "Bitrate": int(bitrate),
                                        "CodingMode": "CODING_MODE_9_1_6",
                                        "SampleRate": 48000,
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }


def build_remux_command(
    mkvmerge_bin,
    output_path,
    source_path,
    video_orders,
    audio_orders,
    text_orders,
    inserts,
):
    """The mkvmerge command inserting .ec3 twins into a file.

    `inserts` is [(audio position, ec3 path, language)], sorted by
    position ascending — position indexes the ORIGINAL audio list, and
    each insert lands AT that position, shifting the rest right. The
    ec3 files are inputs 0..n-1 and the source file is input n, with
    --track-order spelling out the complete final arrangement so
    nothing depends on mkvmerge's own ordering rules. Twins carry no
    default flag; the mkvpropedit pass that follows the remux owns the
    default-track convention.
    """

    source_file_index = len(inserts)
    order = [f"{source_file_index}:{track_order}" for track_order in video_orders]
    audio_entries = [
        f"{source_file_index}:{track_order}" for track_order in audio_orders
    ]
    for input_index, (position, _path, _language) in enumerate(inserts):
        audio_entries.insert(position + input_index, f"{input_index}:0")
    order.extend(audio_entries)
    order.extend(f"{source_file_index}:{track_order}" for track_order in text_orders)

    command = [mkvmerge_bin, "-o", output_path]
    for _position, path, language in inserts:
        command.extend(
            [
                "--language",
                f"0:{language or 'und'}",
                "--track-name",
                "0:E-AC-3 JOC (Atmos) from TrueHD",
                "--default-track-flag",
                "0:0",
                path,
            ]
        )
    command.append(source_path)
    command.extend(["--track-order", ",".join(order)])
    return command


def mediaconvert_client():
    """A MediaConvert client on the app's AWS credentials."""

    return boto3.client(
        "mediaconvert",
        region_name=current_app.config["MEDIACONVERT_REGION"],
        endpoint_url=current_app.config["MEDIACONVERT_ENDPOINT"],
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
    )


def _run_step(command, job, basename, description):
    """Run one pipeline subprocess, streaming its output to the log."""

    from app.videos import wait_for_subprocess

    current_app.logger.info(f"'{basename}' {description}: {command}")
    if job:
        job.meta["description"] = f"'{basename}' — {description}"
        job.save_meta()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    for line in process.stdout:
        line = line.rstrip()
        if line:
            current_app.logger.info(f"'{basename}' {line}")
    wait_for_subprocess(process)


def _wait_for_mediaconvert(client, mc_job_id, job, basename):
    """Poll a MediaConvert job to a terminal state; raise on failure."""

    for _ in range(MEDIACONVERT_POLL_LIMIT):
        mc_job = client.get_job(Id=mc_job_id)["Job"]
        status = mc_job["Status"]
        percent = mc_job.get("JobPercentComplete")
        if job:
            job.meta["description"] = (
                f"'{basename}' — MediaConvert encoding E-AC-3 Atmos"
                + (f" ({percent}%)" if percent is not None else "")
            )
            job.save_meta()
        if status == "COMPLETE":
            return mc_job
        if status in ("ERROR", "CANCELED"):
            raise RuntimeError(
                f"MediaConvert job {mc_job_id} {status}: "
                f"{mc_job.get('ErrorCode')} {mc_job.get('ErrorMessage')}"
            )
        time.sleep(MEDIACONVERT_POLL_SECONDS)
    raise RuntimeError(f"MediaConvert job {mc_job_id} timed out")


def _cleanup_s3_prefix(s3_client, bucket, prefix):
    """Best-effort removal of the pipeline's S3 scratch objects."""

    try:
        listed = s3_client.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/")
        keys = [{"Key": obj["Key"]} for obj in listed.get("Contents", [])]
        if keys:
            s3_client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            current_app.logger.info(
                f"Removed {len(keys)} scratch object(s) under '{prefix}/'"
            )
    except Exception:
        current_app.logger.error(traceback.format_exc())


def maybe_enqueue_atmos_supplement(file_id):
    """Called after an import's track scan: when the file carries a
    TrueHD Atmos track without its E-AC-3 Atmos twin, queue the
    supplement on the transcode queue — serial, so a batch of imports
    converts one film at a time and MediaConvert spend stays visible."""

    rows = (
        FileAudioTrack.query.filter_by(file_id=int(file_id))
        .order_by(FileAudioTrack.track)
        .all()
    )
    tracks = [{"codec": row.codec, "language": row.language} for row in rows]
    if not atmos_supplement_candidates(tracks):
        return False

    file = db.session.get(File, int(file_id))
    if file is None:
        return False
    job_id = safe_job_id(f"atmos_supplement:{file_id}")
    queue = current_app.transcode_queue
    started = StartedJobRegistry(queue=queue)
    if job_id in queue.job_ids or job_id in started.get_job_ids():
        return False
    queue.enqueue(
        "app.atmos.atmos_supplement_task",
        args=(int(file_id),),
        job_timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
        job_id=job_id,
        description=f"E-AC-3 Atmos twin for '{file.basename}'",
    )
    return True


def atmos_supplement_task(file_id):
    """Supplement a file's TrueHD Atmos tracks with E-AC-3 Atmos twins."""

    from app.videos import acquire_lock_or_defer

    with app.app_context():
        file = db.session.get(File, int(file_id))
        if file is None:
            return True

        # Serialize with other tasks that rewrite this title's files or
        # track records

        lock = acquire_lock_or_defer(
            file.file_identifier(),
            current_app.config["TRANSCODE_TASK_TIMEOUT"] * 1000,
            current_app.transcode_scheduler,
            "app.atmos.atmos_supplement_task",
            minutes=(5, 15),
            timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            args=(int(file_id),),
        )
        if not lock:
            return True

        try:
            return _atmos_supplement_unlocked(int(file_id))
        finally:
            current_app.lock_manager.unlock(lock)


def _atmos_supplement_unlocked(file_id):
    """The supplement pipeline; the caller must hold the title's lock."""

    from app.videos import (
        aws_s3_client,
        copy_with_progress,
        flag_possibly_forced_subtitles,
        get_audio_tracks_from_file,
        get_subtitle_tracks_from_file,
        mkvpropedit_unlocked,
        wait_for_subprocess,
        watch_mkvmerge_progress,
    )

    with app.app_context():
        job = get_current_job()
        file = db.session.get(File, int(file_id))
        basename = file.basename
        file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        if not os.path.exists(file_path):
            current_app.logger.warning(f"'{basename}' No local copy, cannot supplement")
            return False

        audio_tracks = get_audio_tracks_from_file(file_path)
        wanting = atmos_supplement_candidates(audio_tracks)
        if not wanting:
            current_app.logger.info(
                f"'{basename}' TrueHD Atmos tracks already have E-AC-3 twins"
            )
            return True

        staging_dir = current_app.config["STAGING_DIR"]
        try:
            staging_free = shutil.disk_usage(staging_dir).free
        except OSError:
            staging_free = 0

        # The workspace holds the remuxed output (≈ the source's size)
        # alongside the .ec3, and earlier the extracted bitstream + DAMF
        # master — the headroom covers those phases

        needed = os.path.getsize(file_path) + WORKSPACE_HEADROOM_BYTES
        if staging_free < needed:
            current_app.logger.warning(
                f"'{basename}' Staging space is insufficient for the Atmos "
                f"pipeline ({staging_free} free, {needed} needed)"
            )
            return False

        workspace = os.path.join(staging_dir, f"atmos-{file_id}")
        s3_client = aws_s3_client()
        bucket = current_app.config["AWS_BUCKET"]
        prefix = f"{current_app.config['AWS_MEDIACONVERT_PREFIX']}/{file_id}"
        inserts = []

        try:
            os.makedirs(workspace, exist_ok=True)

            for source_index in wanting:
                source = audio_tracks[source_index]
                streamorder = source.get("streamorder")

                # Extract the TrueHD bitstream and decode it to a DAMF
                # Atmos master (truehdd; ffmpeg can't produce one)

                thd_path = os.path.join(workspace, f"track{streamorder}.thd")
                _run_step(
                    [
                        current_app.config["MKVEXTRACT_BIN"],
                        "tracks",
                        file_path,
                        f"{streamorder}:{thd_path}",
                    ],
                    job,
                    basename,
                    "Extracting TrueHD bitstream",
                )
                damf_base = os.path.join(workspace, f"track{streamorder}")
                _run_step(
                    [
                        current_app.config["TRUEHDD_BIN"],
                        "decode",
                        "--output-path",
                        damf_base,
                        thd_path,
                    ],
                    job,
                    basename,
                    "Decoding TrueHD to Atmos master",
                )
                damf_files = sorted(glob.glob(f"{damf_base}.atmos*"))
                if not damf_files:
                    raise RuntimeError(
                        f"truehdd produced no DAMF output for '{basename}' "
                        f"track {streamorder}"
                    )
                os.remove(thd_path)

                # Ship the master to S3 scratch and encode it

                for damf_path in damf_files:
                    key = f"{prefix}/{os.path.basename(damf_path)}"
                    size = os.path.getsize(damf_path)
                    current_app.logger.info(
                        f"'{basename}' Uploading {os.path.basename(damf_path)} "
                        f"({size / 1e9:.2f} GB) to s3://{bucket}/{key}"
                    )
                    if job:
                        job.meta["description"] = (
                            f"'{basename}' — Uploading Atmos master to S3"
                        )
                        job.save_meta()
                    s3_client.upload_file(damf_path, bucket, key)

                input_uri = (
                    f"s3://{bucket}/{prefix}/{os.path.basename(damf_base)}.atmos"
                )
                destination_uri = f"s3://{bucket}/{prefix}/out{streamorder}/twin"
                client = mediaconvert_client()
                mc_job = client.create_job(
                    Role=current_app.config["MEDIACONVERT_ROLE_ARN"],
                    Settings=mediaconvert_job_settings(
                        input_uri,
                        destination_uri,
                        current_app.config["EAC3_ATMOS_BITRATE"],
                    ),
                )
                mc_job_id = mc_job["Job"]["Id"]
                current_app.logger.info(
                    f"'{basename}' MediaConvert job {mc_job_id} submitted "
                    f"({current_app.config['EAC3_ATMOS_BITRATE']} bps 9.1.6)"
                )
                _wait_for_mediaconvert(client, mc_job_id, job, basename)

                listed = s3_client.list_objects_v2(
                    Bucket=bucket, Prefix=f"{prefix}/out{streamorder}/"
                )
                ec3_key = next(
                    (
                        obj["Key"]
                        for obj in listed.get("Contents", [])
                        if obj["Key"].endswith(".ec3")
                    ),
                    None,
                )
                if ec3_key is None:
                    raise RuntimeError(
                        f"MediaConvert job {mc_job_id} completed but left no "
                        f".ec3 under '{prefix}/out{streamorder}/'"
                    )
                ec3_path = os.path.join(workspace, f"track{streamorder}.ec3")
                s3_client.download_file(bucket, ec3_key, ec3_path)
                current_app.logger.info(
                    f"'{basename}' Downloaded E-AC-3 Atmos twin "
                    f"({os.path.getsize(ec3_path) / 1e6:.0f} MB)"
                )
                for damf_path in damf_files:
                    os.remove(damf_path)

                inserts.append(
                    (
                        insertion_point(audio_tracks, source_index),
                        ec3_path,
                        source.get("language"),
                    )
                )

            # Remux the twins into place: [E-AC-3 Atmos, FLAC, TrueHD]

            inserts.sort(key=lambda insert: insert[0])
            media_info = MediaInfo.parse(file_path)
            video_orders, audio_orders, text_orders = [], [], []
            for track in media_info.tracks:
                track_order = track.to_data().get("streamorder")
                if not str(track_order).isdigit():
                    continue
                if track.track_type == "Video":
                    video_orders.append(int(track_order))
                elif track.track_type == "Audio":
                    audio_orders.append(int(track_order))
                elif track.track_type == "Text":
                    text_orders.append(int(track_order))

            # Remux onto local staging, not the library share: total SMB
            # traffic is identical (one read of the source, one write of
            # the finished file), but the share never sees a concurrent
            # read+write, the verification parse runs against local
            # disk, and a failed mux can't strand a partial file on the
            # library volume

            staging_output = os.path.join(workspace, basename)
            command = build_remux_command(
                current_app.config["MKVMERGE_BIN"],
                staging_output,
                file_path,
                video_orders,
                audio_orders,
                text_orders,
                inserts,
            )
            current_app.logger.info(f"'{basename}' Running mkvmerge: {command}")
            mkvmerge_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )
            watch_mkvmerge_progress(
                mkvmerge_process, job, basename, "Muxing E-AC-3 Atmos twin"
            )
            wait_for_subprocess(mkvmerge_process, ok_returncodes=(0, 1))

            # Never replace the library copy with a mux that didn't
            # deliver: the output must carry every expected twin and
            # all of the original lossless tracks

            new_audio_tracks = get_audio_tracks_from_file(staging_output)
            originals_before = sum(
                1 for track in audio_tracks if track.get("codec") == TRUEHD_ATMOS_CODEC
            )
            originals_after = sum(
                1
                for track in new_audio_tracks
                if track.get("codec") == TRUEHD_ATMOS_CODEC
            )
            if (
                atmos_supplement_candidates(new_audio_tracks)
                or originals_after != originals_before
            ):
                raise RuntimeError(
                    f"'{basename}' remux did not produce the expected "
                    f"E-AC-3 Atmos twins; library copy left untouched"
                )

            # The verified result crosses to the library as a hidden
            # dotfile, then renames into place; a failed copy is removed
            # so nothing partial ever sits beside the real file

            hidden_output = os.path.join(os.path.dirname(file_path), f".{basename}")
            try:
                copy_with_progress(
                    staging_output,
                    hidden_output,
                    job,
                    basename,
                    "Moving supplemented file into the library",
                )
                os.rename(hidden_output, file_path)
            except BaseException:
                try:
                    os.remove(hidden_output)
                except OSError:
                    pass
                raise
            os.remove(staging_output)

            # Rebuild the track records now that the file changed

            FileAudioTrack.query.filter_by(file_id=file.id).delete()
            FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

            for i, track in enumerate(new_audio_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            new_subtitle_tracks = get_subtitle_tracks_from_file(file_path)
            flag_possibly_forced_subtitles(file, new_subtitle_tracks)
            for i, track in enumerate(new_subtitle_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                db.session.add(FileSubtitleTrack(**track))

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        else:
            db.session.commit()

            # The twin now leads its trio, so the first audio track takes
            # the default flag, and the modified file replaces the
            # untouched S3 archive (Glenn's call: the supplemented MKV is
            # a superset of the rip, and re-downloads must not pay for a
            # second MediaConvert run) — both ride the existing
            # mkvpropedit path, which this task's lock already covers

            try:
                mkvpropedit_unlocked(file.id, 1, None, None)
            except OSError:
                current_app.logger.error(traceback.format_exc())
                raise
            return True

        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            _cleanup_s3_prefix(s3_client, bucket, prefix)
