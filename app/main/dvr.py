"""Virtual DVR channel endpoints (#182): an HDHomeRun-flavored virtual
tuner, the M3U playlist, the XMLTV guide, and the live MPEG-TS streams.

Plex has no native M3U support — its manual "enter its network
address" flow probes <address>/discover.json and expects the
HDHomeRun HTTP protocol (the Channels DVR trick works the same way:
it answers HDHomeRun JSON on its m3u URL). Manual entry skips SSDP
discovery entirely, so the whole protocol here is three JSON
documents: discover.json (device identity), lineup_status.json (no
scanning), and lineup.json (channel number/name/stream URL triples).
The guide is wired separately: Plex's channel-setup step accepts the
XMLTV URL. The lineup's GuideNumber and the guide's channel id pair
the two.

Plex fetches everything with no session cookie, so a secret path
segment gates all of it exactly like the Plex webhook (404 when unset
or wrong, indistinguishable from a missing route).

A stream is an endless chunked response: on connect the schedule math
says what's playing and how far in, ffmpeg joins the file at that
offset paced to real time, and when a program ends the next one spawns
in its place. Transcode runs only while a channel is actually tuned;
the guide and playlist are text rendered from the stored lineups.
"""

import os
import secrets
import subprocess

from datetime import datetime, timezone
from xml.etree import ElementTree

from flask import Response, current_app, request, url_for

from app.dvr import channel_index, channel_lineup, program_at, programs_between
from app.main import bp

# How much guide to publish: a little history so Plex's grid has a
# left edge, two days forward

GUIDE_LOOKBEHIND_SECONDS = 6 * 3600
GUIDE_LOOKAHEAD_SECONDS = 48 * 3600

STREAM_CHUNK_BYTES = 65536

TMDB_POSTER_URL = "https://image.tmdb.org/t/p/w500{poster_path}"


def _authorized(token):
    """Whether the path token matches DVR_TOKEN; always False while the
    feature is unconfigured, so every route 404s."""

    expected = current_app.config["DVR_TOKEN"]
    return bool(expected) and secrets.compare_digest(token, expected)


def _absolute(endpoint, **values):
    """An absolute URL for the endpoint on the host THIS request
    arrived at. SERVER_NAME pins url_for(_external=True) to the public
    hostname, but Plex must get device/stream URLs on the address it
    actually reached us by (loopback on the same machine) — tuning
    through the public host would pull endless MPEG-TS through
    CloudFront."""

    return request.host_url.rstrip("/") + url_for(endpoint, **values)


def _xmltv_time(timestamp):
    """An epoch timestamp as an XMLTV time string in the server's local
    zone (YYYYMMDDHHMMSS +HHMM) — Plex schedules in the viewer's clock,
    and the schedule math is UTC underneath."""

    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .astimezone()
        .strftime("%Y%m%d%H%M%S %z")
    )


@bp.route("/dvr/<token>/discover.json")
@bp.route("/dvr/<token>/playlist.m3u/discover.json")
def dvr_discover(token):
    """The HDHomeRun device-identity document Plex probes when a tuner
    address is entered manually. The playlist.m3u alias tolerates the
    full playlist URL being pasted as the address — Plex concatenates
    /discover.json onto whatever was typed.
    """

    if not _authorized(token):
        return "", 404
    # Derived from the playlist route because it has exactly one URL
    # rule — url_for on this endpoint could build either rule

    base = _absolute("main.dvr_playlist", token=token).rsplit("/playlist.m3u", 1)[0]
    return {
        "FriendlyName": "Fitzflix DVR",
        "Manufacturer": "Fitzflix",
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomeruntc_atsc",
        "FirmwareVersion": "20260831",
        "DeviceID": "FITZFLIX",
        "DeviceAuth": "fitzflix",
        "BaseURL": base,
        "LineupURL": f"{base}/lineup.json",
        "TunerCount": 4,
    }


@bp.route("/dvr/<token>/lineup_status.json")
@bp.route("/dvr/<token>/playlist.m3u/lineup_status.json")
def dvr_lineup_status(token):
    """The HDHomeRun scan-status document: a fixed lineup, no channel
    scanning possible."""

    if not _authorized(token):
        return "", 404
    return {
        "ScanInProgress": 0,
        "ScanPossible": 0,
        "Source": "Cable",
        "SourceList": ["Cable"],
    }


@bp.route("/dvr/<token>/lineup.json")
@bp.route("/dvr/<token>/playlist.m3u/lineup.json")
def dvr_lineup(token):
    """The HDHomeRun channel lineup: number, name, and stream URL per
    channel — how Plex learns what it can tune."""

    if not _authorized(token):
        return "", 404
    return [
        {
            "GuideNumber": str(channel["number"]),
            "GuideName": channel["name"],
            "URL": _absolute("main.dvr_stream", token=token, slug=channel["slug"]),
        }
        for channel in channel_index(current_app.redis)
    ]


@bp.route("/dvr/<token>/playlist.m3u")
def dvr_playlist(token):
    """The M3U tuner playlist: one entry per channel, tvg-id keyed to
    the XMLTV guide, stream URLs carrying the same token."""

    if not _authorized(token):
        return "", 404
    logo = _absolute("static", filename="apple-touch-icon.png")
    lines = ["#EXTM3U"]
    for channel in channel_index(current_app.redis):
        lines.append(
            f'#EXTINF:-1 tvg-id="{channel["slug"]}" tvg-name="{channel["name"]}" '
            f'tvg-chno="{channel["number"]}" tvg-logo="{logo}" '
            f'group-title="Fitzflix",{channel["name"]}'
        )
        lines.append(_absolute("main.dvr_stream", token=token, slug=channel["slug"]))
    return Response("\n".join(lines) + "\n", mimetype="audio/x-mpegurl")


@bp.route("/dvr/<token>/guide.xml")
def dvr_guide(token):
    """The XMLTV guide: every channel's airings from a few hours back
    to two days out, rendered from the stored lineups — the same
    schedule math the streams follow, so guide and stream agree."""

    if not _authorized(token):
        return "", 404
    now = datetime.now(timezone.utc).timestamp()
    start = now - GUIDE_LOOKBEHIND_SECONDS
    stop = now + GUIDE_LOOKAHEAD_SECONDS

    tv = ElementTree.Element("tv", {"generator-info-name": "Fitzflix"})
    lineups = []
    for channel in channel_index(current_app.redis):
        lineup = channel_lineup(current_app.redis, channel["slug"])
        if not lineup:
            continue
        lineups.append(lineup)
        element = ElementTree.SubElement(tv, "channel", {"id": lineup["slug"]})
        ElementTree.SubElement(element, "display-name").text = lineup["name"]
        # The number too: Plex's channel-mapping step pairs the tuner's
        # GuideNumber against these names
        ElementTree.SubElement(element, "display-name").text = str(lineup["number"])

    for lineup in lineups:
        for begins, ends, program in programs_between(lineup, start, stop):
            attributes = {
                "start": _xmltv_time(begins),
                "stop": _xmltv_time(ends),
                "channel": lineup["slug"],
            }
            programme = ElementTree.SubElement(tv, "programme", attributes)
            ElementTree.SubElement(programme, "title").text = program["title"]
            # Episode programs carry the series as title plus these two
            # (.get: movie programs and pre-upgrade lineups lack them)
            if program.get("subtitle"):
                ElementTree.SubElement(programme, "sub-title").text = program[
                    "subtitle"
                ]
            if program.get("episode_num"):
                ElementTree.SubElement(
                    programme, "episode-num", {"system": "onscreen"}
                ).text = program["episode_num"]
            if program["year"]:
                ElementTree.SubElement(programme, "date").text = str(program["year"])
            if program["overview"]:
                ElementTree.SubElement(programme, "desc").text = program["overview"]
            for genre in program["genres"]:
                ElementTree.SubElement(programme, "category").text = genre
            if program["poster_path"]:
                ElementTree.SubElement(
                    programme,
                    "icon",
                    {"src": TMDB_POSTER_URL.format(poster_path=program["poster_path"])},
                )

    document = ElementTree.tostring(tv, encoding="unicode")
    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?>\n{document}',
        mimetype="application/xml",
    )


def _ffmpeg_command(config, path, offset, audio_channels):
    """The ffmpeg invocation for one program: join the file at the
    offset, pace to real time, and emit H.264 + AC-3 in an MPEG-TS mux
    with constant parameters so program boundaries splice cleanly.

    Maps the first video and first audio track — the first audio track
    is always the default track (the house rule).
    """

    bitrate = config["DVR_VIDEO_BITRATE_KBPS"]
    command = [config["FFMPEG_BIN"], "-hide_banner", "-loglevel", "error"]
    if offset > 0:
        command += ["-ss", f"{offset:.3f}"]
    command += [
        "-re",
        "-i",
        path,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "h264_videotoolbox",
        "-b:v",
        f"{bitrate}k",
        "-maxrate",
        f"{bitrate}k",
        "-bufsize",
        f"{bitrate * 2}k",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale='min(1920,iw)':-2",
        "-c:a",
        "ac3",
        "-b:a",
        "448k",
        "-ac",
        str(audio_channels),
        "-f",
        "mpegts",
        "pipe:1",
    ]
    return command


@bp.route("/dvr/<token>/stream/<slug>.ts")
def dvr_stream(token, slug):
    """The channel's live stream: an endless MPEG-TS response that
    starts mid-program wherever the schedule says the channel is now,
    then rolls program to program until the client disconnects.

    ffmpeg spawns on connect and dies with the socket; a program whose
    file is missing or whose transcode dies without output is skipped,
    and a full lap of failures ends the stream rather than spinning.
    """

    if not _authorized(token):
        return "", 404
    lineup = channel_lineup(current_app.redis, slug)
    if not lineup:
        return "", 404

    # The generator outlives this request handler's app context, so it
    # closes over plain values, never current_app

    config = {
        key: current_app.config[key]
        for key in ("FFMPEG_BIN", "LIBRARY_DIR", "DVR_VIDEO_BITRATE_KBPS")
    }
    logger = current_app.logger
    index, offset = program_at(lineup, datetime.now(timezone.utc).timestamp())

    def generate(index, offset):
        programs = lineup["programs"]
        failures = 0
        while failures < len(programs):
            program = programs[index]
            path = os.path.join(config["LIBRARY_DIR"], program["file_path"])
            index = (index + 1) % len(programs)
            if not os.path.isfile(path):
                logger.warning(f"DVR {slug}: missing {path}; skipping")
                offset, failures = 0.0, failures + 1
                continue
            logger.info(f"DVR {slug}: playing {program['title']} @ {offset:.0f}s")
            try:
                process = subprocess.Popen(
                    _ffmpeg_command(config, path, offset, program["audio_channels"]),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                logger.warning(f"DVR {slug}: could not spawn ffmpeg; ending stream")
                return
            produced = False
            try:
                while chunk := process.stdout.read(STREAM_CHUNK_BYTES):
                    produced = True
                    yield chunk
            finally:
                process.kill()
                process.wait()
            failures = 0 if produced else failures + 1
            offset = 0.0
        logger.warning(f"DVR {slug}: no playable programs; ending stream")

    return Response(
        generate(index, offset),
        mimetype="video/mp2t",
        headers={"Cache-Control": "no-store"},
        direct_passthrough=True,
    )
