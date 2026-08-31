"""Inspection aids for the possibly-forced subtitle triage.

Nearly every candidate track is bitmap-format (VobSub/PGS), so text
display is off the table; instead each candidate gets a cue timeline
(first and last cue plus a density strip — sparse clusters read as
forced-shaped, an even spread as commentary-shaped) and a handful of
burned-in snapshot frames, the subtitle overlaid on the movie's own
picture at sampled cue times, which works for bitmap and text tracks
alike.

Generation is proactive: when an import's track scan matches the
possibly-forced heuristic, a task on the transcode queue probes the
cue timestamps (one full container read per track — MKV keeps no
per-track index) and renders the snapshots. The files live outside the
custom-artwork tree so backups ignore them, and they're deleted the
moment they stop being useful: when the file is triaged, or when its
local copy goes away. There is deliberately no orphan sweep.

The runtime-mismatch triage (#234) lives here too: no aids to
generate, just the candidates query over what's already stored.

So does the lossy-audio triage (#212): files whose first audio track
is lossy while a lossless track rides behind. Its inspection aids
(#223) are a loudness-envelope correlation of the ENTIRE tracks —
programme audio correlates near 1.0 with itself across codecs, a
commentary against the programme doesn't — plus listening clips at
sampled points so the ears can confirm what the number claims before
the remux acts on it.
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

# This process's app instance, resolved lazily so the generation task
# can run on a worker without building a second application

app = LocalProxy(get_app)

TIMELINE_BUCKETS = 60

# Snapshot sampling: cue-list quantiles, so the frames spread across
# wherever the cues actually are rather than across the runtime

SNAPSHOT_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def forced_subtitle_candidates(file_id=None):
    """Subtitle tracks that look forced but aren't flagged, grouped by file.

    A track is suspicious when it's unforced and holds a quarter or less
    of the elements of the largest same-language track in the same file —
    the shape of a foreign-parts-only track sitting beside the full
    subtitles. Only meaningful comparisons count (the full track needs at
    least 100 elements, the candidate can't be empty). Files marked
    reviewed are excluded, as are files that already carry a forced track
    — their forced needs are met, so a small unforced sibling is probably
    a commentary or variant.
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


# The runtime triage (#234): a file whose estimated duration disagrees
# hard with its movie's TMDb runtime is usually a title collision at
# capture time — a mislabelled recording matched faithfully — or a
# truncated download. The estimate needs no probe: size over the summed
# track bitrates, all already stored. Thresholds and the short-runtime
# exclusion come straight from the Aug 2026 survey: raw, the check
# flagged 16 of 3,790 files, thirteen of them shorts recorded into
# longer broadcast slots; excluding runtimes of 25 minutes or less left
# the three genuinely wrong files and nothing else.

RUNTIME_RATIO_HIGH = 1.9
RUNTIME_RATIO_LOW = 0.55
RUNTIME_EXCLUDE_MAX_MINUTES = 25


def runtime_mismatch_candidates():
    """Main-feature movie files whose bitrate-estimated duration is far
    off their film's TMDb runtime and that nobody has acknowledged,
    longest overshoot first. The estimate is approximate — a flagged
    file wants an ffprobe before anything acts on it — so the ratio
    does the filtering and the page shows its ingredients."""

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
    """Files whose first audio track is lossy while a lossless track
    rides behind (#212), minus the Atmos pipeline's deliberate trio —
    an E-AC-3 Atmos lead is exactly as wanted — and minus files marked
    reviewed. Same predicates as the Lossy Files report, plus the
    reviewed exclusion that makes this the worklist."""

    # Lazy like the report's copy of this import: atmos resolves the
    # worker app singleton at module import time

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
    """Where one file's triage aids live, outside the custom-artwork
    tree so the nightly backup ignores them."""

    return os.path.join(current_app.config["TRIAGE_SNAPSHOT_DIR"], str(int(file_id)))


def remove_triage_snapshots(file_id):
    """Drop a file's triage aids: called when the file is triaged, or
    when its local copy is deleted or replaced."""

    shutil.rmtree(triage_snapshot_dir(file_id), ignore_errors=True)


def reset_triage_state(file):
    """A replaced file's content is new evidence: any earlier reviewed
    verdict applied to the OLD file, and stale inspection aids picture
    streams that no longer exist. Everything resets on import so the
    file re-earns its way off the triage pages (Glenn's rule: a
    replacement may carry a forced track the original didn't — the
    A Fish Called Wanda case, where an Aug 12 dismissal silently gated
    the file re-imported Aug 18 — and a replacement's length is a new
    length, so the runtime acknowledgement (#234) goes too)."""

    file.subtitle_triage_reviewed = None
    file.runtime_mismatch_reviewed = None
    file.lossy_audio_reviewed = None
    remove_triage_snapshots(file.id)


def _hms(seconds):
    """A H:MM:SS clock string."""

    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _probe_cue_times(file_path, streamorder):
    """Sorted cue start times for one subtitle stream — the single full
    container read per track."""

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
    """The container duration in seconds, or None — a header read, not
    a full scan."""

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
    """The stored timeline: cue count, first/last, and density buckets
    across the runtime."""

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
    """One burned-in frame: input-seek just ahead of the cue with
    timestamps preserved, overlay the subtitle stream, trim to the
    target frame.

    The input seek must land at or before the cue's start so its packet
    demuxes — a bare direct seek to the target (Glenn's direct-seek sketch)
    silently drops the subtitle whenever a keyframe falls between the
    cue and the target, measured live on Speed's trivia track — and
    -copyts keeps original timestamps so the output trim can name the
    target absolutely. The filter graph then decodes only the short
    pre-roll instead of a full second: benchmarked 1.7-7x faster per
    snapshot with byte-identical output at early, mid, and late cues.
    Callers pass `at` slightly after the cue start; the pre-roll here
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
    """Task: build the cue timeline and burned-in snapshots for every
    candidate track of one file. Tracks that already have aids are
    skipped, so re-runs only fill gaps."""

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
    """Called after an import's track scan: when the file matches the
    possibly-forced heuristic, queue snapshot generation on the
    transcode queue (mostly idle, and sized for heavy media reads)."""

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
    """Render-ready aids for one candidate track, or None while they
    haven't been generated: bucket heights as percentages, clock-string
    cue bounds, and the snapshot filenames under the static tree."""

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
# tracks: one ffmpeg pass decodes every track of interest to 8 kHz
# mono (audio-only decode runs ~450x realtime from the SSD, so this
# rides free inside an import), envelopes are RMS loudness per 50 ms
# window, and correlation runs across small alignment lags to absorb
# codec delay. The clips at the sample quantiles exist for LISTENING —
# their per-position numbers are slices of the same full envelopes,
# shown so a local divergence (a diverging credits mix, say) is
# visible next to the clip that plays it.

AUDIO_SAMPLE_QUANTILES = (0.08, 0.25, 0.42, 0.58, 0.75, 0.92)
AUDIO_SAMPLE_SECONDS = 12
AUDIO_DECODE_RATE = 8000
ENVELOPE_WINDOW_SAMPLES = 400
ENVELOPE_WINDOWS_PER_SECOND = AUDIO_DECODE_RATE // ENVELOPE_WINDOW_SAMPLES
ENVELOPE_MAX_LAG = 20
AUDIO_MATCH_THRESHOLD = 0.75


def audio_comparison_dir(file_id):
    """Where one file's lossy-audio comparison lives — a sibling of
    the numeric per-subtitle-track aid directories."""

    return os.path.join(triage_snapshot_dir(file_id), "audio")


def remove_audio_comparison(file_id):
    """Drop a file's audio comparison alone, leaving any pending
    subtitle aids in place."""

    shutil.rmtree(audio_comparison_dir(file_id), ignore_errors=True)


def _extract_audio_clip(file_path, streamorder, at, out_path):
    """One listening clip: a single stream decoded from `at`, downmixed
    to stereo and encoded browser-playable."""

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
    """RMS loudness per 50 ms window of a raw s16le mono decode."""

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
    """Full-track loudness envelopes for several streams, decoded in
    ONE pass over the container — reading the file is the whole cost
    when it lives on the NAS, so every track shares the read."""

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
    """Peak Pearson correlation between two loudness envelopes across
    alignment lags of up to one second, or None when either side is
    too short or flat to compare."""

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
    """Task: correlate the full lossy and lossless tracks of one
    candidate file and cut its listening clips (#223). A file whose
    comparison already exists is left alone, so re-runs are free."""

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

        # Every verdict comes from the FULL tracks, all decoded in one
        # pass over the container

        envelopes = _track_envelopes(
            file_path,
            [first.streamorder] + [track.streamorder for track in lossless_tracks],
        )
        lead_envelope = envelopes.get(first.streamorder, [])

        # The clips exist for listening; the lossy lead is one half of
        # every pair, so it's cut once

        lead_clips = {}
        for number, quantile in enumerate(AUDIO_SAMPLE_QUANTILES, 1):
            at = duration * quantile
            name = f"t{first.track}-{number}.m4a"
            if _extract_audio_clip(
                file_path, first.streamorder, at, os.path.join(out_dir, name)
            ):
                lead_clips[number] = {"at": at, "name": name}

        def local_correlation(other_envelope, at):
            """The clip window's slice of the full envelopes."""

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
    """Called after an import's track scan: when the file lands on the
    lossy-audio worklist (#212), queue clip generation on the
    transcode queue."""

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
    """Render-ready comparison for one candidate file, or None while it
    hasn't been generated: per lossless track, the full-track
    correlation percentage and its verdict, plus the clip pairs with
    clock strings and their local percentages. A comparison written
    before the verdict went full-track falls back to the median of its
    clip correlations."""

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
