"""Serve the virtual DVR channel endpoints (#182).

The endpoints are an HDHomeRun-type virtual tuner, the M3U playlist,
the XMLTV guide, and the live MPEG-TS streams.

Plex has no native M3U support. Its manual "enter its network address"
flow probes <address>/discover.json and expects the HDHomeRun HTTP
protocol. The Channels DVR trick works the same way. It answers
HDHomeRun JSON on its m3u URL. Manual entry skips SSDP discovery
completely. Thus, the whole protocol here is 3 JSON documents:
discover.json (device identity), lineup_status.json (no scanning), and
lineup.json (channel number, name, and stream URL triples). The guide
is connected separately. The channel-setup step of Plex accepts the
XMLTV URL. The GuideNumber of the lineup and the channel id of the
guide pair the two.

Plex fetches everything with no session cookie. Thus, a secret path
segment gates all of it, the same as the Plex webhook. A missing or
wrong token gets a 404. That response is the same as for a missing
route.

A stream is an endless chunked response. On connect, the schedule math
says which program plays and how far in it is. ffmpeg joins the file at
that offset, paced to real time. When a program ends, the next one
spawns in its place. The transcode runs only while a client tunes the
channel. The guide and the playlist are text rendered from the stored
lineups.
"""

import os
import secrets
import subprocess

from datetime import datetime, timezone
from xml.etree import ElementTree

from flask import Response, current_app, request, url_for

from app.dvr import channel_index, channel_lineup, program_at, programs_between
from app.main import bp

# How much guide to publish: some history and 2 days forward. The
# history gives the grid of Plex a left edge.

GUIDE_LOOKBEHIND_SECONDS = 6 * 3600
GUIDE_LOOKAHEAD_SECONDS = 48 * 3600

STREAM_CHUNK_BYTES = 65536

TMDB_POSTER_URL = "https://image.tmdb.org/t/p/w500{poster_path}"


def _authorized(token):
    """Return True if the path token matches DVR_TOKEN.

    The result is always False while the feature is not configured.
    Thus, every route returns a 404."""

    expected = current_app.config["DVR_TOKEN"]
    return bool(expected) and secrets.compare_digest(token, expected)


def _absolute(endpoint, **values):
    """Return an absolute URL for the endpoint on the host of THIS request.

    SERVER_NAME pins url_for(_external=True) to the public hostname. But
    Plex must get the device and stream URLs on the address that it used
    to reach Fitzflix (loopback on the same machine). A tune through the
    public host would pull an endless MPEG-TS stream through CloudFront."""

    return request.host_url.rstrip("/") + url_for(endpoint, **values)


def _xmltv_time(timestamp):
    """Convert an epoch timestamp to an XMLTV time string in the local zone.

    The format is YYYYMMDDHHMMSS +HHMM. Plex schedules in the clock of
    the viewer. The schedule math is UTC underneath."""

    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .astimezone()
        .strftime("%Y%m%d%H%M%S %z")
    )


@bp.route("/dvr/<token>/discover.json")
@bp.route("/dvr/<token>/playlist.m3u/discover.json")
def dvr_discover(token):
    """Return the HDHomeRun device-identity document.

    Plex probes this document when the user enters a tuner address
    manually. The playlist.m3u alias accepts the full playlist URL as
    the address. Plex adds /discover.json to the text that the user
    typed.
    """

    if not _authorized(token):
        return "", 404
    # The base comes from the playlist route because that route has
    # exactly one URL rule. url_for on this endpoint could build one of
    # 2 rules.

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
    """Return the HDHomeRun scan-status document.

    The lineup is fixed. A channel scan is not possible."""

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
    """Return the HDHomeRun channel lineup.

    Each channel has a number, a name, and a stream URL. This is how
    Plex learns which channels it can tune."""

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
    """Return the M3U tuner playlist.

    The playlist has one entry per channel. The tvg-id is the key into
    the XMLTV guide. The stream URLs carry the same token."""

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
    """Return the XMLTV guide.

    The guide has the airings of every channel from some hours back to
    2 days forward. Fitzflix renders it from the stored lineups with the
    same schedule math that the streams use. Thus, the guide and the
    stream agree."""

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
        # Also add the number. The channel-mapping step of Plex pairs the
        # GuideNumber of the tuner against these names.
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
            # An episode program has the series as its title plus these 2
            # fields. Use .get because movie programs and pre-upgrade
            # lineups do not have them.
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
    """Return the ffmpeg command for one program.

    The command joins the file at the offset and paces to real time. It
    emits H.264 and AC-3 in an MPEG-TS mux with constant parameters.
    Thus, the program boundaries splice cleanly.

    It maps the first video track and the first audio track. The first
    audio track is always the default track (the house rule).
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
    """Return the live stream of the channel.

    The stream is an endless MPEG-TS response. It starts in the middle
    of the program that the schedule gives for now. Then it continues
    from program to program until the client disconnects.

    ffmpeg spawns on connect and dies with the socket. The stream skips
    a program if its file is missing or if its transcode dies without
    output. A full lap of failures ends the stream. The stream does not
    spin.
    """

    if not _authorized(token):
        return "", 404
    lineup = channel_lineup(current_app.redis, slug)
    if not lineup:
        return "", 404

    # The generator lives longer than the app context of this request
    # handler. Thus, it closes over plain values, never current_app.

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
