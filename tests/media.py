"""Build the small sample media files of the tests.

make_sample_mkv is the 1 recipe of a test Matroska file. ffmpeg makes
the streams, and mkvmerge sets the languages, the default flags, and the
attachments. A file is built 1 time per session. The next call with the
same path returns it."""

import os
import subprocess


def _tools():
    """Return the paths of ffmpeg and mkvmerge of the test configuration."""

    from tests.conftest import TestConfig

    return TestConfig.FFMPEG_BIN, TestConfig.MKVMERGE_BIN


def make_sample_mkv(
    path,
    audio=(("eng", True),),
    video="h264",
    attachments=(),
    audio_codec="aac",
    subtitles=(),
    size="320x240",
    duration=1,
    truncate=False,
):
    """Build a short Matroska file at path and return path.

    audio holds 1 (language, default) pair for each audio track. Each
    track is a sine tone with its own pitch. video is "h264" (a test
    pattern), "hdr10" (a 10-bit HEVC with HDR10 colors), or "black" (a
    black frame). attachments holds (file path, MIME type) pairs.
    subtitles holds the paths of text subtitle files. With truncate,
    the file is cut as an interrupted copy leaves it: the segment has no
    Duration element, and the end of the file is gone. Then MediaInfo
    gives no General duration (#417). A file that exists already is
    returned as it is."""

    if os.path.exists(path):
        return path
    ffmpeg, mkvmerge = _tools()
    base = f"{path}.base.mkv"

    if video == "black":
        command = [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i"]
        command += [f"color=black:size={size}:duration={duration}:rate=10"]
    else:
        rate = 24 if video == "hdr10" else 10
        command = [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i"]
        command += [f"testsrc=duration={duration}:size={size}:rate={rate}"]
    for number, _ in enumerate(audio):
        frequency = 440 * (number + 1)
        command += ["-f", "lavfi", "-i"]
        command += [f"sine=frequency={frequency}:duration={duration}"]
    for subtitle in subtitles:
        command += ["-i", str(subtitle)]

    command += ["-map", "0:v"]
    for number, _ in enumerate(audio):
        command += ["-map", f"{number + 1}:a"]
    for number, _ in enumerate(subtitles):
        command += ["-map", f"{len(audio) + number + 1}:s"]

    if video == "hdr10":
        command += ["-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-x265-params"]
        command += [
            "hdr10=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc"
            ":max-cll=1000,400:log-level=error"
        ]
    else:
        command += ["-c:v", "libx264"]
    if audio:
        command += ["-c:a", audio_codec]
    if subtitles:
        command += ["-c:s", "srt"]
    if audio:
        command += ["-shortest"]
    command += [base]
    subprocess.run(command, check=True, capture_output=True)

    # The track ids of mkvmerge: 0 is the video, then the audio tracks.

    merge = [mkvmerge, "-o", path]
    for number, (language, default) in enumerate(audio):
        merge += ["--language", f"{number + 1}:{language}"]
        merge += ["--default-track", f"{number + 1}:{1 if default else 0}"]
    for attachment, mime_type in attachments:
        merge += ["--attachment-mime-type", mime_type, "--attach-file", str(attachment)]
    merge += [base]
    subprocess.run(merge, check=True, capture_output=True)
    os.remove(base)
    if truncate:
        _truncate_matroska(path)
    return path


def _truncate_matroska(path, keep=0.6):
    """Remove the segment Duration of a Matroska file, then cut off its end.

    The Duration element (EBML id 0x4489) becomes a Void element of the
    same length. Thus, the other offsets stay valid."""

    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    start = data.find(b"\x44\x89")
    if start < 0:
        raise ValueError(f"{path} has no Duration element")
    total = 3 + (data[start + 2] & 0x7F)
    data[start : start + total] = bytes([0xEC, 0x80 | (total - 2)]) + bytes(total - 2)
    with open(path, "wb") as handle:
        handle.write(data[: int(len(data) * keep)])


def make_sample_mp4(path):
    """Build a 1-second MP4 file (H.264 and AAC audio) at path and return path.

    The import converts a file in a container that is not Matroska. A
    file that exists already is returned as it is."""

    if os.path.exists(path):
        return path
    ffmpeg, _ = _tools()
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i"]
        + ["testsrc=duration=1:size=320x240:rate=10", "-f", "lavfi", "-i"]
        + ["sine=frequency=440:duration=1", "-c:v", "libx264", "-c:a", "aac"]
        + ["-shortest", path],
        check=True,
        capture_output=True,
    )
    return path
