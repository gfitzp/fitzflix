"""Make the inspection aids for the possibly-forced subtitle triage.

Almost all candidate tracks are in a bitmap format (VobSub/PGS). Thus,
a text display is not possible. Instead, each candidate gets a cue
timeline and some snapshot frames. The timeline has the first and the
last cue, plus a density strip. Sparse clusters look like a forced
track. An even spread looks like a commentary track. The snapshot
frames have the subtitle burned into the picture of the movie at
sampled cue times. This works for bitmap and text tracks.

Fitzflix makes the aids in advance. When the track scan of an import
matches the possibly-forced heuristic, a task on the transcode queue
probes the cue timestamps and renders the snapshots. The probe is 1
full container read per track. MKV keeps no per-track index. The
files are outside the custom-artwork tree. Thus, the backups ignore
them. Fitzflix deletes them when they are no longer useful. That is
when the file is triaged, or when its local copy is deleted. By
design, there is no orphan sweep.

The runtime-mismatch triage (#234) is also here. It has no aids to
make. It has only the candidates query over the stored data.

The lossy-audio triage (#212) is also here. Its candidates are the
files whose first audio track is lossy while a lossless track is
behind it. Its inspection aids (#223) are a loudness-envelope
correlation of the ENTIRE tracks, plus listening clips at sampled
points. Programme audio correlates near 1.0 with itself across the
codecs. A commentary does not correlate with the programme. With the
clips, the ears can confirm what the number claims before the remux
acts on it.
"""

import array
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File, FileAudioTrack, FileSubtitleTrack, Movie

# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, the generation task can run on a worker without a second
# application

app = LocalProxy(get_app)

TIMELINE_BUCKETS = 60

# The snapshot sampling uses the quantiles of the cue list. Thus, the
# frames spread across the positions of the cues, not across the runtime

SNAPSHOT_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def forced_subtitle_candidates(file_id=None):
    """Return the subtitle tracks that look forced but have no flag, grouped by file.

    A track is suspicious if it is not forced and has 1 quarter or less
    of the elements of a reference track. The reference track is the
    largest track with the same language in the same file. This is the
    shape of a foreign-parts-only track next to
    the full subtitles. Only meaningful comparisons count. The full
    track needs at least 100 elements. The candidate cannot be empty.
    This function excludes the files marked reviewed. It also excludes
    the files that already have a forced track. Their forced needs are
    met. Thus, a small unforced sibling is probably a commentary or a
    variant.
    """

    ForcedSibling = db.aliased(FileSubtitleTrack)
    sibling_max = (
        db.session.query(
            FileSubtitleTrack.file_id.label("sibling_file_id"),
            FileSubtitleTrack.language.label("sibling_language"),
            db.func.max(FileSubtitleTrack.elements).label("max_elements"),
        )
        .group_by(FileSubtitleTrack.file_id, FileSubtitleTrack.language)
        .subquery()
    )
    query = (
        db.session.query(FileSubtitleTrack, File, sibling_max.c.max_elements)
        .join(
            sibling_max,
            db.and_(
                FileSubtitleTrack.file_id == sibling_max.c.sibling_file_id,
                FileSubtitleTrack.language == sibling_max.c.sibling_language,
            ),
        )
        .join(File, File.id == FileSubtitleTrack.file_id)
        .filter(
            db.or_(
                FileSubtitleTrack.forced == False,
                FileSubtitleTrack.forced.is_(None),
            ),
            ~db.session.query(ForcedSibling.id)
            .filter(
                ForcedSibling.file_id == FileSubtitleTrack.file_id,
                ForcedSibling.forced == True,
            )
            .exists(),
            File.subtitle_triage_reviewed.is_(None),
            sibling_max.c.max_elements >= 100,
            FileSubtitleTrack.elements > 0,
            FileSubtitleTrack.elements * 4 <= sibling_max.c.max_elements,
        )
        .order_by(File.plex_title.asc(), FileSubtitleTrack.track.asc())
    )
    if file_id is not None:
        query = query.filter(File.id == int(file_id))

    grouped = {}
    for track, file, max_elements in query.all():
        entry = grouped.setdefault(file.id, {"file": file, "tracks": []})
        entry["tracks"].append({"track": track, "max_elements": max_elements})
    return list(grouped.values())


# The runtime triage (#234). A file whose estimated duration differs
# much from the TMDb runtime of its movie is usually a title collision
# at capture time. That is a recording with the wrong label, matched
# correctly. Or it is a truncated download. The estimate needs no
# probe. It is the size divided by the sum of the track bitrates. All
# values are already stored. The thresholds and the short-runtime
# exclusion come directly from the survey of 2026-08. Without the
# exclusion, the check flagged 16 of 3790 files. 13 of them were shorts
# recorded into longer broadcast slots. The exclusion of runtimes of
# 25 minutes or less left the 3 files that were really wrong, and no
# others.

RUNTIME_RATIO_HIGH = 1.9
RUNTIME_RATIO_LOW = 0.55
RUNTIME_EXCLUDE_MAX_MINUTES = 25


def runtime_mismatch_candidates():
    """Return the main-feature movie files with a large runtime mismatch.

    The duration estimated from the bitrate differs much from the TMDb
    runtime of the film, and no person acknowledged the file. The
    largest overshoot is first. The estimate is approximate. Run
    ffprobe on a flagged file before an action on it. Thus, the ratio
    does the filtering, and the page shows its inputs."""

    audio_sum = (
        db.session.query(
            FileAudioTrack.file_id.label("file_id"),
            db.func.sum(FileAudioTrack.bitrate_kbps).label("audio_kbps"),
        )
        .group_by(FileAudioTrack.file_id)
        .subquery()
    )
    total_kbps = File.video_bitrate_kbps + db.func.coalesce(audio_sum.c.audio_kbps, 0)
    estimated_minutes = File.filesize_bytes * 8 / 1000 / total_kbps / 60
    ratio = estimated_minutes / Movie.tmdb_runtime

    rows = (
        db.session.query(
            File,
            Movie,
            estimated_minutes.label("estimated_minutes"),
            ratio.label("ratio"),
        )
        .join(Movie, Movie.id == File.movie_id)
        .outerjoin(audio_sum, audio_sum.c.file_id == File.id)
        .filter(
            File.feature_type_id.is_(None),
            File.runtime_mismatch_reviewed.is_(None),
            File.filesize_bytes.isnot(None),
            File.video_bitrate_kbps > 0,
            Movie.tmdb_runtime > RUNTIME_EXCLUDE_MAX_MINUTES,
            db.or_(ratio > RUNTIME_RATIO_HIGH, ratio < RUNTIME_RATIO_LOW),
        )
        .order_by(db.desc("ratio"))
        .all()
    )
    return [
        {
            "file": file,
            "movie": movie,
            "estimated_minutes": float(estimated),
            "ratio": float(flag_ratio),
        }
        for file, movie, estimated, flag_ratio in rows
    ]


def lossy_audio_candidates(file_id=None):
    """Return the files whose first audio track is lossy with a lossless track behind it (#212).

    This excludes the 3-track set that the Atmos pipeline makes by
    design. An E-AC-3 Atmos lead is exactly what is wanted. It also
    excludes the files marked reviewed. The predicates are the same as
    in the Lossy Files report. The reviewed exclusion makes this the
    worklist."""

    # This import is lazy, like the copy of this import in the report.
    # The atmos module resolves the worker app singleton at module import
    # time

    from app.atmos import EAC3_ATMOS_CODEC

    first = db.aliased(FileAudioTrack)
    lossless = db.aliased(FileAudioTrack)
    query = (
        db.session.query(File)
        .join(first, db.and_(first.file_id == File.id, first.track == 1))
        .filter(
            first.compression_mode != "Lossless",
            db.or_(first.codec.is_(None), first.codec != EAC3_ATMOS_CODEC),
            File.lossy_audio_reviewed.is_(None),
            db.session.query(lossless.id)
            .filter(
                lossless.file_id == File.id,
                lossless.compression_mode == "Lossless",
            )
            .exists(),
        )
        .order_by(File.plex_title.asc())
    )
    if file_id is not None:
        query = query.filter(File.id == int(file_id))

    entries = []
    for file in query.all():
        tracks = (
            FileAudioTrack.query.filter_by(file_id=file.id)
            .order_by(FileAudioTrack.track.asc())
            .all()
        )
        entries.append(
            {
                "file": file,
                "tracks": tracks,
                "lossless_tracks": [
                    track for track in tracks if track.compression_mode == "Lossless"
                ],
            }
        )
    return entries


def triage_snapshot_dir(file_id):
    """Return the directory of the triage aids of 1 file.

    It is outside the custom-artwork tree. Thus, the nightly backup
    ignores the aids."""

    return os.path.join(current_app.config["TRIAGE_SNAPSHOT_DIR"], str(int(file_id)))


def remove_triage_snapshots(file_id):
    """Delete the triage aids of a file.

    The caller calls this when the file is triaged, or when its local
    copy is deleted or replaced."""

    shutil.rmtree(triage_snapshot_dir(file_id), ignore_errors=True)


def reset_triage_state(file):
    """Reset the triage state of a replaced file.

    The content of a replaced file is new evidence. An earlier reviewed
    verdict applied to the OLD file. Stale inspection aids show streams
    that no longer exist. Thus, all state resets on import, and the
    file must qualify again to leave the triage pages. Rule from Glenn:
    a replacement can have a forced track that the original did not
    have. This was the A Fish Called Wanda case. A dismissal on
    2026-08-12 silently blocked the file imported again on 2026-08-18.
    The length of a replacement is a new length. Thus, the runtime
    acknowledgement (#234) also resets."""

    file.subtitle_triage_reviewed = None
    file.runtime_mismatch_reviewed = None
    file.lossy_audio_reviewed = None
    remove_triage_snapshots(file.id)


def _hms(seconds):
    """Return a H:MM:SS clock string."""

    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _probe_cue_times(file_path, streamorder):
    """Return the sorted cue start times for 1 subtitle stream.

    This is the single full container read per track."""

    try:
        result = subprocess.run(
            [
                current_app.config["FFPROBE_BIN"],
                "-v",
                "error",
                "-select_streams",
                str(int(streamorder)),
                "-show_entries",
                "packet=pts_time",
                "-of",
                "csv=p=0",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return []
    cues = []
    for line in result.stdout.splitlines():
        value = line.strip().rstrip(",")
        try:
            cues.append(float(value))
        except ValueError:
            continue
    return sorted(cues)


def _probe_duration(file_path):
    """Return the container duration in seconds, or None.

    This is a header read, not a full scan."""

    try:
        result = subprocess.run(
            [
                current_app.config["FFPROBE_BIN"],
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _build_timeline(cues, duration):
    """Return the stored timeline.

    It has the cue count, the first and the last cue, and the density
    buckets across the runtime."""

    span = duration if duration and duration >= cues[-1] else cues[-1] or 1.0
    buckets = [0] * TIMELINE_BUCKETS
    for cue in cues:
        index = min(TIMELINE_BUCKETS - 1, int(cue / span * TIMELINE_BUCKETS))
        buckets[index] += 1
    return {
        "cues": len(cues),
        "first": cues[0],
        "last": cues[-1],
        "duration": span,
        "buckets": buckets,
    }


def _render_snapshot(file_path, streamorder, at, out_path):
    """Render 1 frame with the subtitle burned in.

    This function seeks the input to a point just before the cue and
    keeps the timestamps. It overlays the subtitle stream. Then it
    trims the output to the target frame.

    The input seek must arrive at or before the start of the cue. Then
    its packet demuxes. A direct seek to the target (the direct-seek
    sketch from Glenn) silently drops the subtitle if a keyframe is
    between the cue and the target. This was measured live on the
    trivia track of Speed. -copyts keeps the original timestamps. Thus,
    the output trim can name the target as an absolute time. The
    filter graph then decodes only the short pre-roll, not a full
    second. The benchmark showed 1.7 to 7 times faster per snapshot,
    with byte-identical output at early, middle, and late cues. The
    caller passes `at` a little after the cue start. The pre-roll here
    must stay larger than that offset.
    """

    seek = max(0.0, at - 0.4)
    try:
        subprocess.run(
            [
                current_app.config["FFMPEG_BIN"],
                "-y",
                "-v",
                "error",
                "-ss",
                f"{seek:.3f}",
                "-copyts",
                "-i",
                file_path,
                "-filter_complex",
                f"[0:v][0:{int(streamorder)}]overlay,scale=480:-2",
                "-ss",
                f"{at:.3f}",
                "-frames:v",
                "1",
                "-q:v",
                "4",
                out_path,
            ],
            capture_output=True,
            timeout=300,
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())
    return os.path.isfile(out_path)


def generate_triage_snapshots(file_id):
    """Build the cue timeline and the snapshots for each candidate track of 1 file.

    This task skips the tracks that already have aids. Thus, a second
    run only fills the gaps."""

    with app.app_context():
        file = db.session.get(File, file_id)
        if file is None:
            return True
        file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        if not os.path.isfile(file_path):
            current_app.logger.info(
                f"Triage snapshots: '{file.basename}' is not present locally"
            )
            return True
        entries = forced_subtitle_candidates(file_id=file_id)
        if not entries:
            return True

        duration = _probe_duration(file_path)
        generated = 0
        for item in entries[0]["tracks"]:
            track = item["track"]
            if track.streamorder is None:
                continue
            out_dir = os.path.join(triage_snapshot_dir(file_id), str(track.track))
            if os.path.isfile(os.path.join(out_dir, "timeline.json")):
                continue
            cues = _probe_cue_times(file_path, track.streamorder)
            if not cues:
                continue
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "timeline.json"), "w") as handle:
                json.dump(_build_timeline(cues, duration), handle)
            for number, quantile in enumerate(SNAPSHOT_QUANTILES, 1):
                cue = cues[min(len(cues) - 1, int(quantile * (len(cues) - 1)))]
                _render_snapshot(
                    file_path,
                    track.streamorder,
                    cue + 0.3,
                    os.path.join(out_dir, f"snap-{number}.jpg"),
                )
            generated += 1
        current_app.logger.info(
            f"Triage snapshots: generated aids for {generated} track(s) of "
            f"'{file.basename}'"
        )
        return True


def maybe_enqueue_triage_snapshots(file_id):
    """Queue the snapshot generation if the file matches the possibly-forced heuristic.

    The caller calls this after the track scan of an import. The job
    goes on the transcode queue. That queue is idle most of the time,
    and it is sized for heavy media reads."""

    if not forced_subtitle_candidates(file_id=file_id):
        return False
    file = db.session.get(File, file_id)
    current_app.transcode_queue.enqueue(
        "app.triage.generate_triage_snapshots",
        args=(int(file_id),),
        job_timeout="2h",
        description=f"Subtitle snapshots for '{file.basename}'",
    )
    return True


def triage_presentation(file_id, track_number):
    """Return the render-ready aids for 1 candidate track.

    Return None if the aids do not exist yet. The aids are the bucket
    heights as percentages, the cue bounds as clock strings, and the
    snapshot filenames under the static tree."""

    out_dir = os.path.join(triage_snapshot_dir(file_id), str(int(track_number)))
    timeline_path = os.path.join(out_dir, "timeline.json")
    if not os.path.isfile(timeline_path):
        return None
    try:
        with open(timeline_path) as handle:
            timeline = json.load(handle)
    except Exception:
        return None
    if not isinstance(timeline, dict) or "cues" not in timeline:
        return None

    peak = max(timeline.get("buckets") or [0]) or 1
    return {
        "cues": timeline["cues"],
        "first": _hms(timeline["first"]),
        "last": _hms(timeline["last"]),
        "buckets": [round(count / peak * 100) for count in timeline["buckets"]],
        "snapshots": sorted(
            name
            for name in os.listdir(out_dir)
            if name.startswith("snap-") and name.endswith(".jpg")
        ),
    }


# The lossy-audio comparison (#223). The verdict correlates the ENTIRE
# tracks. One ffmpeg pass decodes each track of interest to 8 kHz mono.
# An audio-only decode runs at approximately 450 times realtime from
# the SSD. Thus, this costs nothing inside an import. The envelopes are
# the RMS loudness per 50 ms window. The correlation runs across small
# alignment lags to absorb the codec delay. The clips at the sample
# quantiles exist for LISTENING. Their per-position numbers are slices
# of the same full envelopes. They are shown so a local difference (for
# example a different credits mix) is visible next to the clip that
# plays it.

AUDIO_SAMPLE_QUANTILES = (0.08, 0.25, 0.42, 0.58, 0.75, 0.92)
AUDIO_SAMPLE_SECONDS = 12
AUDIO_DECODE_RATE = 8000
ENVELOPE_WINDOW_SAMPLES = 400
ENVELOPE_WINDOWS_PER_SECOND = AUDIO_DECODE_RATE // ENVELOPE_WINDOW_SAMPLES
ENVELOPE_MAX_LAG = 20
AUDIO_MATCH_THRESHOLD = 0.75


def audio_comparison_dir(file_id):
    """Return the directory of the lossy-audio comparison of 1 file.

    It is a sibling of the numeric aid directories of the subtitle
    tracks."""

    return os.path.join(triage_snapshot_dir(file_id), "audio")


def remove_audio_comparison(file_id):
    """Delete only the audio comparison of a file.

    This keeps the pending subtitle aids."""

    shutil.rmtree(audio_comparison_dir(file_id), ignore_errors=True)


def _extract_audio_clip(file_path, streamorder, at, out_path):
    """Make 1 listening clip.

    This decodes a single stream from `at`. It downmixes the audio to
    stereo and encodes it in a format that a browser can play."""

    try:
        subprocess.run(
            [
                current_app.config["FFMPEG_BIN"],
                "-y",
                "-v",
                "error",
                "-ss",
                f"{at:.3f}",
                "-i",
                file_path,
                "-map",
                f"0:{int(streamorder)}",
                "-t",
                str(AUDIO_SAMPLE_SECONDS),
                "-ac",
                "2",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                out_path,
            ],
            capture_output=True,
            timeout=600,
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())
    return os.path.isfile(out_path) and os.path.getsize(out_path) > 0


def _pcm_envelope(pcm_path):
    """Return the RMS loudness per 50 ms window of a raw s16le mono decode."""

    with open(pcm_path, "rb") as handle:
        data = handle.read()
    samples = array.array("h")
    samples.frombytes(data[: len(data) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    windows = []
    for start in range(
        0, len(samples) - ENVELOPE_WINDOW_SAMPLES + 1, ENVELOPE_WINDOW_SAMPLES
    ):
        chunk = samples[start : start + ENVELOPE_WINDOW_SAMPLES]
        windows.append(math.sqrt(sum(s * s for s in chunk) / ENVELOPE_WINDOW_SAMPLES))
    return windows


def _track_envelopes(file_path, streamorders):
    """Return the full-track loudness envelopes for several streams.

    This decodes all streams in ONE pass over the container. When the
    file is on the NAS, the file read is the whole cost. Thus, all
    tracks share the read."""

    envelopes = {}
    with tempfile.TemporaryDirectory() as scratch:
        command = [
            current_app.config["FFMPEG_BIN"],
            "-y",
            "-v",
            "error",
            "-i",
            file_path,
        ]
        pcm_paths = {}
        for streamorder in streamorders:
            pcm_path = os.path.join(scratch, f"{int(streamorder)}.pcm")
            command.extend(
                [
                    "-map",
                    f"0:{int(streamorder)}",
                    "-ac",
                    "1",
                    "-ar",
                    str(AUDIO_DECODE_RATE),
                    "-f",
                    "s16le",
                    pcm_path,
                ]
            )
            pcm_paths[streamorder] = pcm_path
        try:
            subprocess.run(command, capture_output=True, timeout=3600)
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return {}
        for streamorder, pcm_path in pcm_paths.items():
            if os.path.isfile(pcm_path):
                envelopes[streamorder] = _pcm_envelope(pcm_path)
    return envelopes


def _envelope_correlation(a, b, max_lag=ENVELOPE_MAX_LAG):
    """Return the peak Pearson correlation between 2 loudness envelopes.

    The peak is across alignment lags of up to 1 second. Return None if
    one side is too short or too flat for a comparison."""

    def pearson(x, y):
        n = len(x)
        if n < 8:
            return None
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        var_x = sum((v - mean_x) ** 2 for v in x)
        var_y = sum((v - mean_y) ** 2 for v in y)
        if var_x <= 0 or var_y <= 0:
            return None
        covariance = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
        return covariance / math.sqrt(var_x * var_y)

    best = None
    for lag in range(-max_lag, max_lag + 1):
        x = a[lag:] if lag >= 0 else a
        y = b if lag >= 0 else b[-lag:]
        n = min(len(x), len(y))
        r = pearson(list(x[:n]), list(y[:n]))
        if r is not None and (best is None or r > best):
            best = r
    return best


def generate_audio_comparison(file_id):
    """Correlate the full lossy and lossless tracks of 1 candidate file (#223).

    This task also cuts the listening clips. It does not change a file
    whose comparison already exists. Thus, a second run costs nothing."""

    with app.app_context():
        file = db.session.get(File, file_id)
        if file is None:
            return True
        file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        if not os.path.isfile(file_path):
            current_app.logger.info(
                f"Audio comparison: '{file.basename}' is not present locally"
            )
            return True
        entries = lossy_audio_candidates(file_id=file_id)
        if not entries:
            return True
        entry = entries[0]
        out_dir = audio_comparison_dir(file_id)
        if os.path.isfile(os.path.join(out_dir, "comparison.json")):
            return True
        duration = _probe_duration(file_path)
        if not duration:
            current_app.logger.warning(
                f"Audio comparison: no duration for '{file.basename}'"
            )
            return True

        first = entry["tracks"][0]
        if first.streamorder is None:
            return True
        lossless_tracks = [
            track for track in entry["lossless_tracks"] if track.streamorder is not None
        ]
        if not lossless_tracks:
            return True
        os.makedirs(out_dir, exist_ok=True)

        # Each verdict comes from the FULL tracks. One pass over the
        # container decodes all of them

        envelopes = _track_envelopes(
            file_path,
            [first.streamorder] + [track.streamorder for track in lossless_tracks],
        )
        lead_envelope = envelopes.get(first.streamorder, [])

        # The clips exist for listening. The lossy lead is one half of
        # each pair. Thus, Fitzflix cuts it 1 time

        lead_clips = {}
        for number, quantile in enumerate(AUDIO_SAMPLE_QUANTILES, 1):
            at = duration * quantile
            name = f"t{first.track}-{number}.m4a"
            if _extract_audio_clip(
                file_path, first.streamorder, at, os.path.join(out_dir, name)
            ):
                lead_clips[number] = {"at": at, "name": name}

        def local_correlation(other_envelope, at):
            """Return the slice of the full envelopes for the clip window."""

            start = int(at * ENVELOPE_WINDOWS_PER_SECOND)
            span = AUDIO_SAMPLE_SECONDS * ENVELOPE_WINDOWS_PER_SECOND
            return _envelope_correlation(
                lead_envelope[start : start + span],
                other_envelope[start : start + span],
            )

        pairs = []
        for lossless in lossless_tracks:
            lossless_envelope = envelopes.get(lossless.streamorder, [])
            samples = []
            for number, lead in sorted(lead_clips.items()):
                name = f"t{lossless.track}-{number}.m4a"
                if not _extract_audio_clip(
                    file_path,
                    lossless.streamorder,
                    lead["at"],
                    os.path.join(out_dir, name),
                ):
                    continue
                samples.append(
                    {
                        "at": lead["at"],
                        "lossy": lead["name"],
                        "lossless": name,
                        "correlation": local_correlation(lossless_envelope, lead["at"]),
                    }
                )
            pairs.append(
                {
                    "lossy_track": first.track,
                    "lossless_track": lossless.track,
                    "correlation": _envelope_correlation(
                        lead_envelope, lossless_envelope
                    ),
                    "samples": samples,
                }
            )

        with open(os.path.join(out_dir, "comparison.json"), "w") as handle:
            json.dump({"pairs": pairs}, handle)
        current_app.logger.info(
            f"Audio comparison: generated {len(pairs)} pair(s) for "
            f"'{file.basename}'"
        )
        return True


def maybe_enqueue_audio_comparison(file_id):
    """Queue the clip generation if the file is on the lossy-audio worklist (#212).

    The caller calls this after the track scan of an import. The job
    goes on the transcode queue."""

    if not lossy_audio_candidates(file_id=file_id):
        return False
    file = db.session.get(File, file_id)
    current_app.transcode_queue.enqueue(
        "app.triage.generate_audio_comparison",
        args=(int(file_id),),
        job_timeout="2h",
        description=f"Audio comparison for '{file.basename}'",
    )
    return True


def lossy_audio_presentation(file_id):
    """Return the render-ready comparison for 1 candidate file.

    Return None if the comparison does not exist yet. For each lossless
    track, the result has the full-track correlation percentage and
    its verdict, plus the clip pairs with clock strings and their local
    percentages. A comparison written before the verdict became
    full-track uses the median of its clip correlations instead."""

    comparison_path = os.path.join(audio_comparison_dir(file_id), "comparison.json")
    if not os.path.isfile(comparison_path):
        return None
    try:
        with open(comparison_path) as handle:
            comparison = json.load(handle)
    except Exception:
        return None
    pairs = comparison.get("pairs") if isinstance(comparison, dict) else None
    if not isinstance(pairs, list):
        return None

    for pair in pairs:
        correlations = []
        for sample in pair.get("samples", []):
            sample["clock"] = _hms(sample["at"])
            if sample.get("correlation") is not None:
                sample["percent"] = round(sample["correlation"] * 100)
                correlations.append(sample["correlation"])
            else:
                sample["percent"] = None
        overall = pair.get("correlation")
        if overall is None and correlations:
            overall = sorted(correlations)[len(correlations) // 2]
            pair["percent"] = None
        else:
            pair["percent"] = round(overall * 100) if overall is not None else None
        pair["verdict"] = (
            None
            if overall is None
            else "match" if overall >= AUDIO_MATCH_THRESHOLD else "differs"
        )
    return {"pairs": pairs}
