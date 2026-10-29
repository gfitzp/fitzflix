"""Add an E-AC-3 Atmos twin to each TrueHD Atmos track (#55b).

Apple TV clients cannot pass through lossless TrueHD. Thus, the Atmos
mix of a disc plays without its spatial layer. Thus, each TrueHD Atmos
track gets an E-AC-3 JOC (DD+ Atmos) twin. Infuse passes the twin
through with Atmos intact. ffmpeg has no JOC encoder. Thus, the twin
comes from AWS MediaConvert. mkvextract pulls the TrueHD bitstream.
truehdd decodes it to a DAMF Atmos master. MediaConvert encodes
EAC3_ATMOS 9.1.6 at the 1024k limit of the encoder. mkvmerge inserts
the .ec3 before the FLAC twin. Thus, the 3 tracks read [E-AC-3 Atmos,
FLAC, TrueHD Atmos].

Fitzflix validated the full pipeline on Ghost in the Shell (1995). All
15 dynamic objects and the LFE bed were intact. The duration matched
exactly. MediaConvert bills by the minute (about $0.85 for each feature
film). The pass is idempotent. A file that has its twins plans nothing.
Thus, a re-import never pays 2 times. DAMF intermediate files are about
8 GB for each film. The pipeline removes them from staging and from S3
if the job succeeds or fails.
"""

import contextlib
import glob
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from flask import current_app
from pymediainfo import MediaInfo
from rq import get_current_job
from rq.registry import StartedJobRegistry

from app import s3_layout
from app.tasks import enqueue_task, free_job_id, report_progress
from app import db, get_app, safe_job_id
from app.models import File, FileAudioTrack
from app.naming import hidden
from app.settings import get_setting
from app.pipeline import TaskPhases, close_trail, keep_trail_open
from app.tracks import stop_process
from app.plex_library import enqueue_plex_analyze

app = get_app()

# The MediaInfo commercial names, as stored in file_audio_track.codec.

TRUEHD_ATMOS_CODEC = "Dolby TrueHD with Dolby Atmos"
EAC3_ATMOS_CODEC = "Dolby Digital Plus with Dolby Atmos"

# atmos_check_task asks MediaConvert about a run this often, and gives
# up after this many checks (6 hours).

MEDIACONVERT_CHECK_SECONDS = 120
MEDIACONVERT_CHECK_LIMIT = 180

# The local scratch requirement: the extracted TrueHD bitstream plus
# its decoded DAMF master, with headroom.

WORKSPACE_HEADROOM_BYTES = 16 * 1024**3

# An import keeps its staged copy for the Atmos job only while staging
# keeps this much space free. Atmos jobs run 1 at a time. In a large
# batch, the kept copies wait on staging. New imports need about 2.5
# times their size free there.

STAGED_HANDOFF_RESERVE_BYTES = 200 * 1024**3


def staged_handoff(staged_path, library_path):
    """Return the staged copy of an import for its Atmos job, or None.

    The import copied the file from staging into the library. The staged
    copy is byte-identical. The Atmos job can start from it, and does
    not read the file back from the NAS. The result records the size
    and the modification time of the library file. Thus, the job can
    see a later change to the library file. This returns None if the
    staged copy is missing or staging is short of space."""

    if not staged_path:
        return None
    try:
        staged_size = os.path.getsize(staged_path)
        free = shutil.disk_usage(os.path.dirname(staged_path)).free
        library = os.stat(library_path)
    except OSError:
        return None
    if free < STAGED_HANDOFF_RESERVE_BYTES:
        current_app.logger.info(
            f"'{os.path.basename(library_path)}' Staging has {free} bytes free. "
            f"The Atmos job will copy from the library"
        )
        return None
    if library.st_size != staged_size:
        return None
    return {
        "path": staged_path,
        "size": library.st_size,
        "mtime": library.st_mtime,
    }


def discard_staged_source(staged_source):
    """Remove a staged copy that the Atmos job does not use."""

    if staged_source and staged_source.get("path"):
        try:
            os.remove(staged_source["path"])
        except OSError:
            pass


def _usable_staged_source(staged_source, library_path):
    """Return True if the staged copy still matches the library file.

    Something can change the library file after the import, for example
    a track flag edit. A start from the old staged copy would then undo
    that change. Thus, the size and the modification time of the library
    file must be the same as at the import."""

    if not staged_source:
        return False
    try:
        library = os.stat(library_path)
        staged_size = os.path.getsize(staged_source["path"])
    except (OSError, KeyError, TypeError):
        return False
    return library.st_size == staged_source.get(
        "size"
    ) == staged_size and library.st_mtime == staged_source.get("mtime")


def atmos_supplement_candidates(audio_tracks):
    """Return the indices of the TrueHD Atmos tracks that have no E-AC-3
    Atmos twin.

    This matches by count for each language, like the FLAC presence
    rule. Thus, a file that already has its twins (or an S3 re-download
    of it) plans nothing. The pass is idempotent."""

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
    """Return the audio-list position where the E-AC-3 twin of a source
    goes.

    If a FLAC twin directly precedes the source, the position is before
    the FLAC twin. That is the pair shape of the rip profile. If not,
    the position is directly before the source."""

    if source_index > 0:
        previous = audio_tracks[source_index - 1]
        if previous.get("format") == "FLAC" and previous.get(
            "language"
        ) == audio_tracks[source_index].get("language"):
            return source_index - 1
    return source_index


def mediaconvert_job_settings(input_uri, destination_uri, bitrate):
    """Return the validated MediaConvert job shape.

    The input is 1 DAMF Atmos master. The output is 1 raw .ec3, coded
    9.1.6."""

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
    dropped_text_orders=(),
):
    """Return the mkvmerge command that inserts .ec3 twins into a file.

    `inserts` is [(audio position, ec3 path, language)], sorted by
    position in ascending order. The position indexes the ORIGINAL
    audio list. Each insert goes AT that position. The rest move to the
    right. The ec3 files are inputs 0..n-1. The source file is input n.
    The --track-order option gives the complete final arrangement.
    Thus, nothing depends on the ordering rules of mkvmerge. The twins
    carry no default flag. The mkvpropedit pass after the remux applies
    the default-track convention. dropped_text_orders are the subtitle
    tracks with zero elements. The command leaves them out. Before, a
    second in-place remux removed them and needed a third copy of
    space (#518).
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
    if dropped_text_orders:
        command.extend(
            ["-s", ",".join(str(track_order) for track_order in text_orders)]
            if text_orders
            else ["--no-subtitles"]
        )
    command.append(source_path)
    command.extend(["--track-order", ",".join(order)])
    return command


def mediaconvert_client():
    """Return a MediaConvert client that uses the AWS credentials of the app."""

    return boto3.client(
        "mediaconvert",
        region_name=current_app.config["MEDIACONVERT_REGION"],
        endpoint_url=current_app.config["MEDIACONVERT_ENDPOINT"],
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
    )


def _set_stage(job, basename, description, progress=-1):
    """Show a pipeline stage on the job card.

    The description and the progress always change together. Thus, a
    new stage never shows the old percentage of the previous stage. A
    progress of -1 shows the indeterminate bar until the stage reports
    real numbers."""

    report_progress(job, basename, description, progress)


class _UploadProgress:
    """Sum the byte progress of a multi-file S3 upload.

    The job card shows the progress as whole percents. boto3 calls the
    callback from its transfer threads. Thus, the count takes a lock."""

    def __init__(self, job, total_bytes):
        self.job = job
        self.total = max(int(total_bytes), 1)
        self.seen = 0
        self.last_percent = -1
        self.lock = threading.Lock()

    def __call__(self, transferred):
        with self.lock:
            self.seen += transferred
            percent = min(int(self.seen * 100 / self.total), 100)
            if percent != self.last_percent and self.job:
                self.last_percent = percent
                self.job.meta["progress"] = percent
                self.job.save_meta()


def _run_step(command, job, basename, description, ok_returncodes=(0,)):
    """Run 1 pipeline subprocess.

    This streams the output of the subprocess to the log. It sends each
    percentage that the subprocess prints to the job card. One example
    is the "Progress: N%" lines of mkvextract."""

    from app.videos import wait_for_subprocess

    current_app.logger.info(f"'{basename}' {description}: {command}")
    _set_stage(job, basename, description)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    previous_percent = None
    try:
        for line in process.stdout:
            line = line.rstrip()
            if not line:
                continue
            current_app.logger.info(f"'{basename}' {line}")
            percent_match = re.search(r"(\d{1,3})%", line)
            if percent_match and job:
                percent = min(int(percent_match.group(1)), 100)
                if percent != previous_percent:
                    previous_percent = percent
                    job.meta["progress"] = percent
                    job.save_meta()
    except BaseException:
        # The child must not keep writing into a workspace that the
        # cleanup removes (#499).
        stop_process(process)
        raise
    wait_for_subprocess(process, ok_returncodes=ok_returncodes)


def _cleanup_s3_prefix(s3_client, bucket, prefix):
    """Try to remove the S3 scratch objects of the pipeline, with all versions.

    The bucket keeps versions. A delete without a version id only adds a
    delete marker, and the old version stays in S3 Standard. On
    2026-10-24, 1.6 TB of masters stayed that way. Thus, this deletes
    each version and each delete marker. The lifecycle rule of the
    scratch prefix (aws_setup) removes what a failed cleanup leaves."""

    try:
        doomed = []
        paginator = s3_client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            for entry in page.get("Versions", []) + page.get("DeleteMarkers", []):
                doomed.append({"Key": entry["Key"], "VersionId": entry["VersionId"]})
        s3_layout.delete_versions(s3_client, bucket, doomed)
        if doomed:
            current_app.logger.info(
                f"Removed {len(doomed)} scratch object version(s) under '{prefix}/'"
            )
    except Exception:
        current_app.logger.error(traceback.format_exc())


class FinishInterrupted(Exception):
    """A transient fault stopped the finish before the library file changed (#502).

    The download, the remux and the library copy can fail for a short
    time (a dead remount, a network fault). A later try can succeed.
    The paid encode stays in the S3 scratch area for the retry."""


# The finish tries again after a transient fault, up to this number of
# times, after FINISH_RETRY_DELAY each time. Then the run ends.

MAX_FINISH_RETRIES = 3
FINISH_RETRY_DELAY = timedelta(minutes=10)


@contextlib.contextmanager
def _transient_step(step):
    """Raise FinishInterrupted for an I/O or AWS fault inside the step.

    A tool that exits with an error (corrupt input) gives the same error
    again. Thus, its error is final. Only a tool that a stop signal ended
    can try again (#502). A lost database connection at the stale flag
    of the library copy is transient as well (#561 follow-up)."""

    import botocore.exceptions
    from sqlalchemy.exc import OperationalError

    from app.importing import INTERRUPT_SIGNALS

    try:
        yield
    except subprocess.CalledProcessError as error:
        if -(error.returncode or 0) in INTERRUPT_SIGNALS:
            raise FinishInterrupted(f"{step}: {error!r}") from error
        raise
    except (
        OSError,
        OperationalError,
        botocore.exceptions.BotoCoreError,
        botocore.exceptions.ClientError,
    ) as error:
        raise FinishInterrupted(f"{step}: {error!r}") from error


class AtmosRunEnded(RuntimeError):
    """The run ended on purpose: an encode failed, or the checks ran out."""


def _cancel_encodes(job_ids):
    """Cancel the MediaConvert encodes of a failed run.

    Else they continue to bill, and they write their outputs after the
    cleanup. A finished or failed encode refuses the cancel. That is
    not an error."""

    for job_id in job_ids:
        try:
            mediaconvert_client().cancel_job(Id=job_id)
            current_app.logger.info(f"Canceled MediaConvert job {job_id}")
        except Exception as e:
            current_app.logger.info(f"MediaConvert job {job_id} not canceled: {e}")


# The Atmos run is 3 jobs. While MediaConvert encodes, no job runs, and
# no worker waits. This key marks a run that is between its jobs. Then
# a second request for the same file does not start a second run.

CHAIN_KEY = "fitzflix:atmos:chain:{file_id}"

# The own chip of the run on the trail of the file. The check and the
# finish jobs write to the chips of the first job.

OWN_STAGE = "Adding Atmos twin"

# A sentinel result of _atmos_prepare: staging is short of space now.

NO_STAGING_SPACE = "no staging space"


def maybe_enqueue_atmos_supplement(file_id, staged_source=None, queue=None):
    """Queue the supplement if the file needs an E-AC-3 Atmos twin.

    The import calls this after its track scan. If the file carries a
    TrueHD Atmos track without its E-AC-3 Atmos twin, this queues the
    first job of the supplement on queue. The default is the import
    queue, for the library sweep of "flask audio atmos". The import
    gives the handoff queue. The file is already in the pipeline. Thus,
    its twin does not wait behind the new imports (Glenn, 2026-10-17).
    The run is 3 jobs.
    atmos_supplement_task decodes the track and submits it to
    MediaConvert. atmos_check_task asks MediaConvert about the encode
    every 2 minutes. atmos_finish_task muxes the twin in and moves the
    file into the library. Thus, no worker waits on MediaConvert, and
    more than 1 film can encode at a time (Glenn, 2026-10-15).

    staged_source is the staged copy of a new import (staged_handoff).
    The job starts from it and does not read the library file back.
    This returns True only if it queued the job. Then the job owns the
    staged copy. Otherwise the caller removes it."""

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
    if current_app.redis.exists(CHAIN_KEY.format(file_id=int(file_id))):
        return False
    job_id = safe_job_id(f"atmos_supplement:{file_id}")
    for queued in (
        current_app.handoff_queue,
        current_app.import_queue,
        current_app.transcode_queue,
    ):
        started = StartedJobRegistry(queue=queued)
        if job_id in queued.job_ids or job_id in started.get_job_ids():
            return False

    # A finished job under the fixed id gives its expiry to a new job
    # under the same id (#504). The queued job then expires before a
    # worker takes it. A live job keeps the id.

    if not free_job_id("app.atmos.atmos_supplement_task", job_id):
        return False
    enqueue_task(
        "app.atmos.atmos_supplement_task",
        int(file_id),
        **({"staged_source": staged_source} if staged_source else {}),
        queue=queue or current_app.import_queue,
        description=f"E-AC-3 Atmos twin for '{file.basename}'",
        job_id=job_id,
    )
    return True


def _origin_queue():
    """Return the queue of the current job, or the import queue.

    A retry goes back to the queue that its job came from. Thus, the
    twin of a new import stays on the handoff queue, and a library
    sweep stays on the import queue."""

    job = get_current_job()
    for queue in (current_app.handoff_queue, current_app.transcode_queue):
        if job is not None and job.origin == queue.name:
            return queue
    return current_app.import_queue


def atmos_supplement_task(file_id, staged_source=None):
    """Decode the TrueHD Atmos tracks of a file and submit them to MediaConvert.

    This is the first of the 3 jobs of the supplement. It takes the
    title lock and gives it to atmos_check_task, with the state of the
    run. The chips of this job stay open until atmos_finish_task closes
    them. staged_source is the staged copy that the import gave to this
    job, or None. The deferred retries carry it. When the job ends, a
    staged copy that it did not use is removed."""

    from app.tmdb_refresh import tmdb_refresh_pending
    from app.videos import acquire_lock_or_defer, schedule_retry

    with app.app_context():
        file = db.session.get(File, int(file_id))
        if file is None:
            discard_staged_source(staged_source)
            return True

        retry_kwargs = {"staged_source": staged_source} if staged_source else None
        queue = _origin_queue()

        # The import queues the TMDB refresh and this task together. The
        # refresh must apply first. It can rename the title, and it holds
        # the title lock for seconds. The run holds the lock until the
        # finish job. Thus, this task waits while a refresh of the movie
        # is still on its way.

        if file.movie_id and tmdb_refresh_pending("Movies", file.movie_id):
            schedule_retry(
                queue,
                "app.atmos.atmos_supplement_task",
                (1, 3),
                current_app.config["TRANSCODE_TASK_TIMEOUT"],
                f"'{file.basename}'",
                "A TMDB refresh of the movie is pending",
                args=(int(file_id),),
                kwargs=retry_kwargs,
                key=int(file_id),
            )
            return True

        # Serialize with the other tasks that rewrite the files or the
        # track records of this title.

        lock = acquire_lock_or_defer(
            file.file_identifier(),
            current_app.config["TRANSCODE_TASK_TIMEOUT"] * 1000,
            queue,
            "app.atmos.atmos_supplement_task",
            minutes=(5, 15),
            timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            args=(int(file_id),),
            kwargs=retry_kwargs,
            key=int(file_id),
        )
        if not lock:
            return True

        # Each step of the run shows as a chip on the trail of the file.
        # A failure turns the chip of its step red.

        phases = TaskPhases()
        handed_off = False
        outcome = None
        try:
            outcome = _atmos_prepare(int(file_id), phases, staged_source)
            if isinstance(outcome, dict):
                _start_checks(outcome, lock)
                handed_off = True
                return True
        except BaseException:
            phases.fail()
            raise
        finally:
            if not handed_off:
                current_app.lock_manager.unlock(lock)
                # False is "no local copy". An upload of a file that is
                # not there only marks the row stale for nothing.
                if outcome is not False and outcome != NO_STAGING_SPACE:
                    archive_after_early_end(file_id)

            # A used staged copy is in the workspace now. This removes an
            # unused one.

            discard_staged_source(staged_source)

        phases.finish()
        if outcome == NO_STAGING_SPACE:
            # More than 1 run can use staging now. Wait for space. Do not
            # give up on the twin.

            schedule_retry(
                queue,
                "app.atmos.atmos_supplement_task",
                (15, 30),
                current_app.config["TRANSCODE_TASK_TIMEOUT"],
                f"'{file.basename}'",
                "Staging space is insufficient for the Atmos pipeline",
                args=(int(file_id),),
                key=int(file_id),
            )
            return True
        return outcome


def _atmos_prepare(file_id, phases, staged_source=None):
    """Stage the file, decode each TrueHD Atmos track, and submit the encodes.

    This returns the state of the run for atmos_check_task, or True or
    False when the run ends here, or NO_STAGING_SPACE. The library share
    carries 1 read here (the staging copy), and none if the import gave
    its staged copy. The DAMF masters are removed after their upload.
    Thus, the workspace holds only the staged source during the encode.
    """

    from app.tracks import replace_track_rows
    from app.videos import (
        aws_s3_client,
        copy_with_progress,
        get_audio_tracks_from_file,
        get_subtitle_tracks_from_file,
    )

    job = get_current_job()
    file = db.session.get(File, int(file_id))
    basename = file.basename
    file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    if not os.path.exists(file_path):
        current_app.logger.warning(f"'{basename}' No local copy, cannot supplement")
        return False

    # A cheap check of the stored rows before the full-size staging
    # copy. The pipeline inspects the copy again as the real authority.

    stored_rows = (
        FileAudioTrack.query.filter_by(file_id=file.id)
        .order_by(FileAudioTrack.track)
        .all()
    )
    stored_tracks = [
        {"codec": row.codec, "language": row.language} for row in stored_rows
    ]
    if stored_rows and not atmos_supplement_candidates(stored_tracks):
        current_app.logger.info(
            f"'{basename}' TrueHD Atmos tracks already have E-AC-3 twins"
        )
        return True

    staging_dir = current_app.config["STAGING_DIR"]
    try:
        staging_free = shutil.disk_usage(staging_dir).free
    except OSError:
        staging_free = 0

    # The workspace holds the staged source AND the remuxed output for a
    # short time. Each is about the size of the source. The headroom
    # covers the extracted bitstream, the DAMF master, and the .ec3
    # phases. A staged copy from the import is already on staging. Then
    # only the output needs new space.

    use_staged = _usable_staged_source(staged_source, file_path)
    if staged_source and not use_staged:
        current_app.logger.info(
            f"'{basename}' The library file changed after the import. "
            f"Copying it from the library"
        )
        discard_staged_source(staged_source)
    copies = 1 if use_staged else 2
    needed = copies * os.path.getsize(file_path) + WORKSPACE_HEADROOM_BYTES
    if staging_free < needed:
        current_app.logger.warning(
            f"'{basename}' Staging space is insufficient for the Atmos "
            f"pipeline ({staging_free} free, {needed} needed). Trying again later"
        )
        return NO_STAGING_SPACE

    workspace = os.path.join(staging_dir, f"atmos-{file_id}")
    s3_client = aws_s3_client()
    bucket = current_app.config["AWS_BUCKET"]
    prefix = f"{current_app.config['AWS_MEDIACONVERT_PREFIX']}/{file_id}"
    tracks = []

    try:
        os.makedirs(workspace, exist_ok=True)
        staging_source = os.path.join(workspace, basename)
        if use_staged:
            # The import left this copy on the same SSD. The move into the
            # workspace is a rename. The NAS is not read.

            try:
                os.rename(staged_source["path"], staging_source)
                current_app.logger.info(
                    f"'{basename}' Using the staged copy from the import"
                )
            except OSError:
                use_staged = False
        if not use_staged:
            phases.enter("Copying to staging")
            copy_with_progress(
                file_path, staging_source, job, basename, "Copying to local staging"
            )

        audio_tracks = get_audio_tracks_from_file(staging_source)
        wanting = atmos_supplement_candidates(audio_tracks)
        if not wanting:
            # The stored rows said that a twin is missing, but the file
            # has its twins. A finish that failed after its rename leaves
            # this state (#503). The rows follow the staged copy now.
            # Without this, each sweep copied the whole file again.

            current_app.logger.info(
                f"'{basename}' TrueHD Atmos tracks already have E-AC-3 twins. "
                f"Rebuilding the track rows from the file"
            )
            # The copy took long inside the read snapshot of the row. A
            # new transaction avoids MariaDB error 1020.
            subtitle_tracks = get_subtitle_tracks_from_file(staging_source)
            db.session.rollback()
            file = db.session.get(File, int(file_id))
            replace_track_rows(file, audio_tracks, subtitle_tracks)
            db.session.commit()
            shutil.rmtree(workspace, ignore_errors=True)
            return True

        for source_index in wanting:
            source = audio_tracks[source_index]
            streamorder = source.get("streamorder")

            # Extract the TrueHD bitstream and decode it to a DAMF Atmos
            # master with truehdd. ffmpeg cannot produce one.

            thd_path = os.path.join(workspace, f"track{streamorder}.thd")
            phases.enter("Decoding TrueHD")
            # mkvextract exits 1 after a warning when the extraction
            # continued, as the other mkvtoolnix calls accept (#518)
            _run_step(
                [
                    current_app.config["MKVEXTRACT_BIN"],
                    "tracks",
                    staging_source,
                    f"{streamorder}:{thd_path}",
                ],
                job,
                basename,
                "Extracting TrueHD bitstream",
                ok_returncodes=(0, 1),
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

            # Upload the master to the S3 scratch area and submit the
            # encode. The run does not wait for it here.

            phases.enter("Encoding E-AC-3 Atmos")
            _set_stage(job, basename, "Uploading Atmos master to S3", progress=0)
            upload_progress = _UploadProgress(
                job, sum(os.path.getsize(path) for path in damf_files)
            )
            for damf_path in damf_files:
                key = f"{prefix}/{os.path.basename(damf_path)}"
                size = os.path.getsize(damf_path)
                current_app.logger.info(
                    f"'{basename}' Uploading {os.path.basename(damf_path)} "
                    f"({size / 1e9:.2f} GB) to s3://{bucket}/{key}"
                )
                s3_client.upload_file(damf_path, bucket, key, Callback=upload_progress)

            input_uri = f"s3://{bucket}/{prefix}/{os.path.basename(damf_base)}.atmos"
            destination_uri = f"s3://{bucket}/{prefix}/out{streamorder}/twin"
            mc_job = mediaconvert_client().create_job(
                Role=current_app.config["MEDIACONVERT_ROLE_ARN"],
                Settings=mediaconvert_job_settings(
                    input_uri,
                    destination_uri,
                    get_setting("EAC3_ATMOS_BITRATE"),
                ),
            )
            mc_job_id = mc_job["Job"]["Id"]

            # Record the job before the removes (#504). A fault in a
            # remove must still cancel the encode. Else it bills to the
            # end and writes into a prefix that the cleanup emptied.

            tracks.append(
                {
                    "streamorder": streamorder,
                    "insert_at": insertion_point(audio_tracks, source_index),
                    "language": source.get("language"),
                    "mc_job_id": mc_job_id,
                }
            )
            current_app.logger.info(
                f"'{basename}' MediaConvert job {mc_job_id} submitted "
                f"({get_setting('EAC3_ATMOS_BITRATE')} bps 9.1.6)"
            )
            for damf_path in damf_files:
                os.remove(damf_path)

    except BaseException:
        current_app.logger.error(traceback.format_exc())
        shutil.rmtree(workspace, ignore_errors=True)
        _cancel_encodes([track["mc_job_id"] for track in tracks])
        _cleanup_s3_prefix(s3_client, bucket, prefix)
        raise

    return {
        "file_id": file.id,
        "basename": basename,
        "workspace": workspace,
        "staging_source": staging_source,
        "bucket": bucket,
        "prefix": prefix,
        "audio_tracks": audio_tracks,
        "tracks": tracks,
        "trail_job": job.id if job else None,
        "chain": uuid.uuid4().hex[:8],
    }


def _start_checks(state, lock):
    """Give the run to atmos_check_task. Keep the chips of this job open.

    The worker stamps a job done when it returns. The run is not done.
    Thus, the trail_continues flag tells the worker to leave the chip of
    this job open."""

    job = get_current_job()
    try:
        if job is not None:
            job.meta["trail_continues"] = True
            job.save_meta()
        current_app.redis.set(
            CHAIN_KEY.format(file_id=state["file_id"]),
            state["chain"],
            ex=current_app.config["TRANSCODE_TASK_TIMEOUT"],
        )

        # rq removes the hash of this job soon after it returns. The trail
        # healing must not take the open chips of the run for orphans.

        keep_trail_open(
            current_app.redis,
            state["trail_job"],
            current_app.config["TRANSCODE_TASK_TIMEOUT"],
        )
        _book_check(state, lock, 0, None)
    except Exception:
        # No check job owns the run (#518). The encodes billed on, the
        # masters and the workspace stayed, and the chain key refused a
        # new run and the workspace sweep for 2 days. The caller releases
        # the lock and queues the early-end archive.
        _cancel_encodes([track["mc_job_id"] for track in state["tracks"]])
        _close_run(state, None, ended_early=False)
        if job is not None:
            job.meta["trail_continues"] = False
            job.save_meta()
        raise


def _continuation_meta(state, word):
    """Return the job meta of a check or finish job of a run.

    trail_job puts the row of the job on the trail of the first job.
    scheduled_word names the wait of a booked check on the queue page."""

    return {"trail_job": state["trail_job"], "scheduled_word": word}


def _book_check(state, lock, checks, percent):
    """Book the next atmos_check_task in MEDIACONVERT_CHECK_SECONDS."""

    progress = f" ({percent}%)" if percent is not None else ""
    enqueue_task(
        "app.atmos.atmos_check_task",
        state,
        lock,
        checks=checks,
        delay=timedelta(seconds=MEDIACONVERT_CHECK_SECONDS),
        job_id=safe_job_id(f"atmos_check:{state['file_id']}:{state['chain']}:{checks}"),
        description=(
            f"'{state['basename']}' — MediaConvert encoding E-AC-3 Atmos{progress}"
        ),
        meta=_continuation_meta(state, "checks again"),
    )


def _close_run(state, lock, ended_early=True):
    """Release the title lock and remove the workspace and the S3 scratch area.

    A run that ended before its archive step queues the archive of a
    conversion (#505). The import left that upload to the run."""

    from app.videos import aws_s3_client

    if ended_early:
        archive_after_early_end(state["file_id"])

    shutil.rmtree(state["workspace"], ignore_errors=True)
    try:
        _cleanup_s3_prefix(aws_s3_client(), state["bucket"], state["prefix"])
    except Exception:
        current_app.logger.error(traceback.format_exc())
    current_app.redis.delete(CHAIN_KEY.format(file_id=state["file_id"]))
    close_trail(current_app.redis, state["trail_job"])
    if lock:
        current_app.lock_manager.unlock(lock)


def archive_after_early_end(file_id):
    """Queue the transcoded/ archive of a conversion whose Atmos run ended early (#505).

    The import skips that upload while an Atmos job is queued. The run
    archives the file at its end. Some runs end before that point: no
    local copy, a MediaConvert error, a remux fault, the cancel after
    the last check. They left the conversion with no transcoded/
    archive."""

    try:
        file = db.session.get(File, int(file_id))
        # A current transcoded/ archive needs no new upload.
        current = bool(
            file is not None
            and file.aws_transcoded_key
            and file.aws_transcoded_date_uploaded
            and not file.aws_stale
        )
        if (
            file is not None
            and file.converted_from_quality
            and not current
            and get_setting("ARCHIVE_ORIGINAL_MEDIA")
        ):
            enqueue_task(
                "app.videos.upload_task", file.id, description=f"'{file.basename}'"
            )
    except Exception:
        current_app.logger.error(traceback.format_exc())


# A workspace with no run is stale after this time with no change.

STALE_WORKSPACE_SECONDS = 24 * 3600


def sweep_stale_atmos_workspaces():
    """Remove each staging/atmos-<id> workspace that no run owns (#505). Return the paths.

    A run owns its workspace while its chain key exists. It also owns it
    while the title lock of its file is held (the decode before the
    chain starts).
    A workspace also stays while a file in it changed in the last
    STALE_WORKSPACE_SECONDS. A workspace holds a full staged copy, tens
    of GB, that nothing else removes."""

    staging_dir = current_app.config["STAGING_DIR"]
    removed = []
    try:
        names = os.listdir(staging_dir)
    except OSError:
        return removed
    now = time.time()
    for name in names:
        match = re.fullmatch(r"atmos-(\d+)", name)
        path = os.path.join(staging_dir, name)
        if not match or not os.path.isdir(path):
            continue
        file_id = int(match.group(1))
        if current_app.redis.exists(CHAIN_KEY.format(file_id=file_id)):
            continue
        file = db.session.get(File, file_id)
        if file is not None and current_app.redis.exists(file.file_identifier()):
            continue
        try:
            newest = max(
                [os.stat(path).st_mtime]
                + [
                    os.stat(os.path.join(path, entry)).st_mtime
                    for entry in os.listdir(path)
                ]
            )
        except OSError:
            continue
        if now - newest < STALE_WORKSPACE_SECONDS:
            continue
        shutil.rmtree(path, ignore_errors=True)
        current_app.logger.warning(f"'{path}' Removed a stale Atmos workspace")
        removed.append(path)
    return removed


def _trail_phases(state, current):
    """Return the phases of the run, bound to the chips of the first job."""

    return TaskPhases(
        target=(state["trail_job"], state["basename"], OWN_STAGE), current=current
    )


def atmos_check_task(state, lock, checks=0):
    """Ask MediaConvert once about the encodes of an Atmos run.

    This holds a worker for about 1 second. Then it books itself again
    in MEDIACONVERT_CHECK_SECONDS. When each encode is complete, it
    queues atmos_finish_task. A network error only books the next
    check. An encode that fails, or a run longer than
    MEDIACONVERT_CHECK_LIMIT checks, ends the run. Any other fault
    books the next check too (#505). Without it, the chain had no owner:
    the lock, the encodes and the workspace stayed for days."""

    with app.app_context():
        try:
            return _check_once(state, lock, checks)
        except AtmosRunEnded:
            raise
        except Exception:
            current_app.logger.error(traceback.format_exc())
            if checks + 1 < MEDIACONVERT_CHECK_LIMIT:
                try:
                    _book_check(state, lock, checks + 1, None)
                    return True
                except Exception:
                    current_app.logger.error(traceback.format_exc())
            _trail_phases(state, None).close("failed")
            _cancel_encodes([track["mc_job_id"] for track in state["tracks"]])
            _close_run(state, lock)
            raise


def _check_once(state, lock, checks):
    """Ask MediaConvert 1 time, and book the next step of the run."""

    job = get_current_job()
    basename = state["basename"]
    try:
        client = mediaconvert_client()
        results = [
            client.get_job(Id=track["mc_job_id"])["Job"] for track in state["tracks"]
        ]
    except Exception as e:
        current_app.logger.warning(
            f"'{basename}' MediaConvert status unavailable ({e}). " f"Asking again"
        )
        results = None

    percent = None
    if results is not None:
        failed = [
            result
            for result in results
            if result.get("Status") in ("ERROR", "CANCELED")
        ]
        if failed:
            _trail_phases(state, "Encoding E-AC-3 Atmos").fail()
            _trail_phases(state, None).close("failed")
            _cancel_encodes(
                [
                    result.get("Id")
                    for result in results
                    if result.get("Status") not in ("COMPLETE", "ERROR", "CANCELED")
                ]
            )
            _close_run(state, lock)
            raise AtmosRunEnded(
                "; ".join(
                    f"MediaConvert job {result.get('Id')} "
                    f"{result.get('Status')}: {result.get('ErrorCode')} "
                    f"{result.get('ErrorMessage')}"
                    for result in failed
                )
            )
        percent = min(
            (
                100
                if result.get("Status") == "COMPLETE"
                else int(result.get("JobPercentComplete") or 0)
            )
            for result in results
        )
        _set_stage(job, basename, "MediaConvert encoding E-AC-3 Atmos", percent)
        if all(result.get("Status") == "COMPLETE" for result in results):
            current_app.logger.info(
                f"'{basename}' MediaConvert finished the E-AC-3 Atmos twin"
            )
            enqueue_task(
                "app.atmos.atmos_finish_task",
                state,
                lock,
                description=f"E-AC-3 Atmos twin for '{basename}'",
                job_id=safe_job_id(f"atmos_finish:{state['file_id']}:{state['chain']}"),
                meta=_continuation_meta(state, "retries"),
            )
            return True

    if checks + 1 >= MEDIACONVERT_CHECK_LIMIT:
        _trail_phases(state, "Encoding E-AC-3 Atmos").fail()
        _trail_phases(state, None).close("failed")
        _cancel_encodes([track["mc_job_id"] for track in state["tracks"]])
        _close_run(state, lock)
        raise AtmosRunEnded(
            f"MediaConvert did not finish the encode of '{basename}' "
            f"after {checks + 1} checks"
        )
    _book_check(state, lock, checks + 1, percent)
    return True


def atmos_finish_task(state, lock, finish_retries=0):
    """Mux the finished E-AC-3 Atmos twins in and move the file into the library.

    This is the last of the 3 jobs. It releases the title lock and
    removes the workspace and the S3 scratch area. It closes the chips
    of the first job. A transient fault before the library file changes
    books the finish again (#502). The retry keeps the lock, the
    workspace and the encode. finish_retries counts the retries."""

    with app.app_context():
        phases = _trail_phases(state, "Encoding E-AC-3 Atmos")
        try:
            result = _atmos_finish(state, phases)
        except FinishInterrupted as error:
            if finish_retries < MAX_FINISH_RETRIES and _book_finish_retry(
                state, lock, finish_retries + 1, error
            ):
                return False
            phases.fail()
            phases.close("failed")
            _close_run(state, lock)
            raise
        except BaseException:
            phases.fail()
            phases.close("failed")
            _close_run(state, lock)
            raise
        _close_run(state, lock, ended_early=False)
        phases.finish()
        phases.close("done")
        return result


def _book_finish_retry(state, lock, retries, error):
    """Book atmos_finish_task again after FINISH_RETRY_DELAY. Return True if it is booked."""

    current_app.logger.warning(
        f"'{state['basename']}' The Atmos finish stopped ({error}). Trying again "
        f"in {int(FINISH_RETRY_DELAY.total_seconds() // 60)} minutes (attempt "
        f"{retries} of {MAX_FINISH_RETRIES})"
    )
    try:
        current_app.redis.expire(
            CHAIN_KEY.format(file_id=state["file_id"]),
            current_app.config["TRANSCODE_TASK_TIMEOUT"],
        )
        enqueue_task(
            "app.atmos.atmos_finish_task",
            state,
            lock,
            finish_retries=retries,
            delay=FINISH_RETRY_DELAY,
            description=f"E-AC-3 Atmos twin for '{state['basename']}'",
            job_id=safe_job_id(
                f"atmos_finish:{state['file_id']}:{state['chain']}:{retries}"
            ),
            meta=_continuation_meta(state, "retries"),
        )
    except Exception:
        current_app.logger.error(traceback.format_exc())
        return False
    return True


def _remux_twins(state, phases, inserts, job):
    """Remux the downloaded twins into the staged copy and verify the result.

    Return (audio tracks, subtitle tracks) of the output. The verified
    output takes the name of the staged copy, and state["remuxed"]
    becomes True. A tool or I/O fault raises FinishInterrupted (#502). A
    failed verification raises RuntimeError."""

    from app.videos import (
        get_audio_tracks_from_file,
        get_subtitle_tracks_from_file,
        wait_for_subprocess,
        watch_mkvmerge_progress,
    )

    basename = state["basename"]
    workspace = state["workspace"]
    staging_source = state["staging_source"]
    audio_tracks = state["audio_tracks"]
    phases.enter("Remuxing")

    # Remux the twins into place: [E-AC-3 Atmos, FLAC, TrueHD]. This
    # reads the staged source and writes next to it. All of this is
    # local.

    inserts.sort(key=lambda insert: insert[0])
    media_info = MediaInfo.parse(staging_source)
    video_orders, audio_orders, text_orders = [], [], []
    empty_text_orders, unknown_text = [], False
    for media_track in media_info.tracks:
        data = media_track.to_data()
        track_order = data.get("streamorder")
        if not str(track_order).isdigit():
            unknown_text = unknown_text or media_track.track_type == "Text"
            continue
        if media_track.track_type == "Video":
            video_orders.append(int(track_order))
        elif media_track.track_type == "Audio":
            audio_orders.append(int(track_order))
        elif media_track.track_type == "Text":
            elements = data.get("count_of_elements")
            if str(elements).isdigit() and int(elements) == 0:
                empty_text_orders.append(int(track_order))
            else:
                text_orders.append(int(track_order))

    # The mux leaves out each zero-element subtitle track (#518). A
    # subtitle track with an unknown id makes the selection unsafe, as
    # in remove_empty_subtitle_tracks. Then every track stays.

    if unknown_text:
        text_orders = sorted(text_orders + empty_text_orders)
        empty_text_orders = []

    staging_output = hidden(os.path.join(workspace, basename))
    command = build_remux_command(
        current_app.config["MKVMERGE_BIN"],
        staging_output,
        staging_source,
        video_orders,
        audio_orders,
        text_orders,
        inserts,
        dropped_text_orders=empty_text_orders,
    )
    current_app.logger.info(f"'{basename}' Running mkvmerge: {command}")
    with _transient_step("remux"):
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

    # The first audio track gets the default flag. Each other track
    # has the flag cleared. This is the library convention. The
    # pipeline applies it while the file is on the local disk.

    total_audio = len(audio_orders) + len(inserts)
    flag_args = []
    for track_number in range(1, total_audio + 1):
        flag_args.extend(
            [
                "--edit",
                f"track:a{track_number}",
                "--set",
                f"flag-default={'1' if track_number == 1 else '0'}",
            ]
        )
    with _transient_step("remux"):
        _run_step(
            [current_app.config["MKVPROPEDIT_BIN"], staging_output] + flag_args,
            job,
            basename,
            "Setting default audio flag",
            ok_returncodes=(0, 1),
        )

    # Never replace the library copy with a mux that failed. The
    # output must carry each expected twin and all of the original
    # lossless tracks.

    new_audio_tracks = get_audio_tracks_from_file(staging_output)
    new_subtitle_tracks = get_subtitle_tracks_from_file(staging_output)
    originals_before = sum(
        1 for track in audio_tracks if track.get("codec") == TRUEHD_ATMOS_CODEC
    )
    originals_after = sum(
        1 for track in new_audio_tracks if track.get("codec") == TRUEHD_ATMOS_CODEC
    )
    if (
        atmos_supplement_candidates(new_audio_tracks)
        or originals_after != originals_before
    ):
        raise RuntimeError(
            f"'{basename}' remux did not produce the expected "
            f"E-AC-3 Atmos twins; library copy left untouched"
        )

    # The staged source is not necessary now. The verified output
    # takes its name. Thus, the untouched-archive upload gets the same
    # S3 key that the library file would get.

    with _transient_step("remux"):
        os.replace(staging_output, staging_source)

    # A retry of the finish starts after this point (#502). The staged
    # copy is the verified output now.

    state["remuxed"] = True
    return new_audio_tracks, new_subtitle_tracks


def _atmos_finish(state, phases):
    """Download the twins, remux, verify, and replace the library file.

    The library share carries 1 write here: the verified result."""

    from app.aws_storage import archive_library_file, mark_archive_stale
    from app.tracks import flag_archive_before_rewrite, replace_track_rows
    from app.videos import (
        aws_s3_client,
        copy_with_progress,
        get_audio_tracks_from_file,
        get_subtitle_tracks_from_file,
        record_filesize,
    )

    job = get_current_job()
    file = db.session.get(File, int(state["file_id"]))
    if file is None:
        return True
    basename = state["basename"]
    file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    workspace = state["workspace"]
    staging_source = state["staging_source"]
    s3_client = aws_s3_client()
    bucket = state["bucket"]
    prefix = state["prefix"]
    inserts = []

    final_staging = staging_source
    try:
        if state.get("remuxed"):
            # A retry after the remux (#502). The staged copy is the
            # verified output already.
            new_audio_tracks = get_audio_tracks_from_file(final_staging)
            new_subtitle_tracks = get_subtitle_tracks_from_file(final_staging)
        for track in [] if state.get("remuxed") else state["tracks"]:
            streamorder = track["streamorder"]
            with _transient_step("download"):
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
                    f"MediaConvert job {track['mc_job_id']} completed but left "
                    f"no .ec3 under '{prefix}/out{streamorder}/'"
                )
            ec3_path = os.path.join(workspace, f"track{streamorder}.ec3")
            _set_stage(job, basename, "Downloading E-AC-3 Atmos twin")
            with _transient_step("download"):
                s3_client.download_file(bucket, ec3_key, ec3_path)
            current_app.logger.info(
                f"'{basename}' Downloaded E-AC-3 Atmos twin "
                f"({os.path.getsize(ec3_path) / 1e6:.0f} MB)"
            )
            inserts.append((track["insert_at"], ec3_path, track["language"]))

        if not state.get("remuxed"):
            new_audio_tracks, new_subtitle_tracks = _remux_twins(
                state, phases, inserts, job
            )

        # The 1 write that the library share carries. The verified result
        # crosses as a hidden dotfile. Then the pipeline renames it into
        # place. The pipeline removes a failed copy. Thus, no partial file
        # sits next to the real file.

        hidden_output = hidden(os.path.join(os.path.dirname(file_path), basename))
        phases.enter("Replacing the library file")

        # The flag below rolls the session back. Thus, read the id first.

        file_id = file.id
        with _transient_step("library copy"):
            try:
                copy_with_progress(
                    final_staging,
                    hidden_output,
                    job,
                    basename,
                    "Replacing the library file with the supplemented file",
                )

                # A stop between the rename and the commit leaves the
                # twin in the library and the old rows (#561). The flag
                # makes the weekly sync upload the file. A good archive
                # below clears it.

                flag_archive_before_rewrite(file_id)
                os.rename(hidden_output, file_path)
            except BaseException:
                try:
                    os.remove(hidden_output)
                except OSError:
                    pass
                raise

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        raise

    # The library file carries the twins now. Rebuild the track records
    # because the file changed. The run took minutes since the File row
    # was read. A new transaction avoids MariaDB error 1020 when a
    # triage click or an upload changed it. The size comes from the
    # staging copy. It is byte-identical to the file that was renamed
    # into the library. Thus, the share is not touched again.

    try:
        db.session.rollback()
        file = db.session.get(File, file_id)
        replace_track_rows(file, new_audio_tracks, new_subtitle_tracks)
        record_filesize(file, os.path.getsize(final_staging))
        file.date_updated = datetime.now(timezone.utc)
        file.note_track_edit()
        db.session.commit()
    except Exception:
        # The archive is behind the library file now (#503). The weekly
        # sync uploads a stale archive. The rows get 1 more try. Without
        # it, the next sweep copies the whole file again and changes
        # nothing. A second failure fails the job.

        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        mark_archive_stale(file_id, reason="the Atmos twin replaced the library file")
        file = db.session.get(File, file_id)
        replace_track_rows(file, new_audio_tracks, new_subtitle_tracks)
        record_filesize(file, os.path.getsize(final_staging))
        # The first commit rolled back. Its count of the edit went too.
        file.note_track_edit()
        db.session.commit()

    # The film gained a track under a path that Plex already knows. Thus,
    # nothing tells Plex to look again until its next scan. Ask for the
    # analysis now instead (#194).

    enqueue_plex_analyze(file_path)

    # The supplemented library file goes to modified/, or to transcoded/
    # for a conversion (#299). The rip in untouched/ never changes. A
    # restore from modified/ goes straight into the library. Thus, it never
    # pays for a second MediaConvert run (Glenn). The upload reads the
    # staging copy, not the library share. The library replacement above
    # is already committed. Thus, an upload failure fails the job. The old
    # key still has a date, and the S3 sync cannot see that it is behind.
    # Thus, the failure marks the archive stale, and the sync uploads it.

    if get_setting("ARCHIVE_ORIGINAL_MEDIA"):
        phases.enter("Archiving to S3")
        try:
            archive_library_file(file, final_staging)
        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            mark_archive_stale(
                int(state["file_id"]), reason="re-archive after an Atmos supplement"
            )
            raise
    return True
