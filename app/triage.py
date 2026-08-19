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
"""

import json
import os
import shutil
import subprocess
import traceback

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File, FileSubtitleTrack

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


def triage_snapshot_dir(file_id):
    """Where one file's triage aids live, outside the custom-artwork
    tree so the nightly backup ignores them."""

    return os.path.join(current_app.config["TRIAGE_SNAPSHOT_DIR"], str(int(file_id)))


def remove_triage_snapshots(file_id):
    """Drop a file's triage aids: called when the file is triaged, or
    when its local copy is deleted or replaced."""

    shutil.rmtree(triage_snapshot_dir(file_id), ignore_errors=True)


def reset_triage_state(file):
    """A replaced file's subtitle content is new evidence: any earlier
    reviewed verdict applied to the OLD tracks, and stale inspection
    aids picture streams that no longer exist. Both reset on import so
    the file re-earns its way off the triage page (#74, Glenn's rule:
    a replacement may carry a forced track the original didn't — the
    A Fish Called Wanda case, where an Aug 12 dismissal silently gated
    the file re-imported Aug 18)."""

    file.subtitle_triage_reviewed = None
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
    demuxes — a bare direct seek to the target (Glenn's #38 sketch)
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
