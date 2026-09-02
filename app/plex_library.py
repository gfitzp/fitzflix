"""Refresh the Plex libraries and empty their trash, safely.

This module replaces the external cron. That cron ran curl for refresh
and emptyTrash on hardcoded section ids. It checked one mount per
section as a guard. The guard is the whole point. If the directory of a
section is missing (for example, an SMB mount dropped), a scan marks
every item as missing. Then the empty of the trash deletes the metadata
of the library, with the watch states included. Plex rebuilds the
library from zero when the mount returns.

This version asks Plex for the OWN location paths of each movie and
show section. It requires the MOUNT of every location to be alive
before it touches that section. The old script checked one mount per
section. But the Movies section has 2 roots on 2 volumes. The guard is
at the mount level, not at the leaf level. An empty transcodes volume
correctly has no Movies subfolder. A missing leaf on a healthy mount is
safe to scan. The mount probes go through volume_alive. That function
treats a hung SMB stat as dead. It does not hang the worker.

This module also owns the generic re-analyze. Every task that rewrites
a library file IN PLACE calls enqueue_plex_analyze. Then Plex reads the
file again immediately. It does not wait for its own scan and its
overnight analysis pass. The manual analyze of Plex reads the streams
again and regenerates the chapter thumbs. This takes approximately 2
seconds on a 45 GB film (#194).

This module does NOT touch the Media-level audioCodec and audioChannels
summary. Plex shows that summary as the audio of an item. A measurement
on 2026-08-24 over 3,153 films showed that the summary is the audio
track of the file with the HIGHEST channel count. It matched that track
100% of the time. It matched the first (default) track only 94.6% of
the time, that is, only when the two were the same track. Thus, a
supplemented film reads "FLAC 7.1" while its lossless twin outranks the
DD+ Atmos 5.1. An analyze, by this task or by hand in Plex, does not
change that.
"""

import hashlib
import os
import traceback
from datetime import timedelta

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import get_app, retry_job_id, safe_job_id
from app.maintenance import VOLUMES_ROOT, volume_alive
from app.plex_titles import _plex_get

app = LocalProxy(get_app)

PAGE_SIZE = 1000

# The item type to page for in each kind of section. In a movie section,
# the items carry the files. In a show section, the episodes carry them.

SECTION_ITEM_TYPE = {"movie": 1, "show": 4}

# Plex cannot analyze a file that it has not scanned yet. To keep the
# analyze, one deferred attempt follows the next quarter-hourly library
# scan.

ANALYZE_RETRY_MINUTES = 20
ANALYZE_MAX_RETRIES = 1


def _probe_target(path):
    """Return the path to health-check for a section location.

    This is the /Volumes mount that backs the location. If no volume
    backs it, this is the path itself."""

    if path.startswith(VOLUMES_ROOT + os.sep):
        return "/".join(path.split("/")[:3])
    return path


def _section_locations(section):
    """Return the paths that a section declares as its roots."""

    return [
        location.get("path")
        for location in section.get("Location", []) or []
        if location.get("path")
    ]


def _part_files(item):
    """Yield every part file path that the media of an item declares."""

    for media in item.get("Media", []) or []:
        for part in media.get("Part", []) or []:
            if part.get("file"):
                yield part["file"]


def _plex_command(method, path):
    """Make a command-style Plex call (refresh, emptyTrash).

    These calls answer with an EMPTY body. Thus, this function parses
    nothing. The status is the success signal."""

    r = requests.request(
        method,
        current_app.config["PLEX_URL"] + path,
        params={"X-Plex-Token": current_app.config["PLEX_TOKEN"]},
        timeout=60,
    )
    r.raise_for_status()


def refresh_plex_libraries():
    """Scan each movie and show section for changes and empty its trash.

    This is a queue task. It works per section. It touches a section only
    when every location that the section declares is mounted and
    present."""

    with app.app_context():
        if not (
            current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
        ):
            return True

        try:
            payload = _plex_get("/library/sections")
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return True

        for section in payload.get("MediaContainer", {}).get("Directory", []) or []:
            if section.get("type") not in ("movie", "show"):
                continue
            key = section.get("key")
            title = section.get("title")
            locations = _section_locations(section)

            # A dead or hung mount makes a scan dangerous. Skip the whole
            # section until the mount is back.

            missing = [
                path
                for path in locations
                if not volume_alive(_probe_target(path))
                or not os.path.isdir(_probe_target(path))
            ]
            if missing or not locations:
                current_app.logger.warning(
                    f"Plex library '{title}': skipping refresh — mount(s) "
                    f"unavailable for: {missing or 'no declared locations'}"
                )
                continue

            try:
                _plex_command("GET", f"/library/sections/{key}/refresh")
                _plex_command("PUT", f"/library/sections/{key}/emptyTrash")
                current_app.logger.info(
                    f"Plex library '{title}': scanned and emptied trash"
                )
            except Exception:
                current_app.logger.warning(traceback.format_exc())
        return True


def _sections_holding(sections, wanted):
    """Return the sections that can hold these files.

    A section owns a wanted path when one of its declared locations is a
    parent of that path.

    This filter prevents the analyze of a movie from paging the whole TV
    section. Nothing matches when Plex mounts the library at paths that
    this host does not share. Then this function returns every movie and
    show section instead, because the part-file basenames can still
    identify the items.
    """

    owning = [
        section
        for section in sections
        if any(
            path.startswith(location.rstrip("/") + os.sep)
            for location in _section_locations(section)
            for path in wanted
        )
    ]
    return owning or sections


def _analyze_section(section, targets, by_basename, analyzed):
    """Analyze every target file that this section holds.

    This function adds each matched file to `analyzed`. It matches a
    file on its full path. If Plex knows the library by a different
    path, it matches the file on its basename."""

    key = section.get("key")
    start = 0
    while True:
        payload = _plex_get(
            f"/library/sections/{key}/all",
            params={
                "type": SECTION_ITEM_TYPE[section["type"]],
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": PAGE_SIZE,
            },
        )
        container = payload.get("MediaContainer", {})
        page = container.get("Metadata", []) or []
        for item in page:
            rating_key = item.get("ratingKey")
            if not rating_key:
                continue
            for path in _part_files(item):
                target = (
                    os.path.abspath(path)
                    if os.path.abspath(path) in targets
                    else by_basename.get(os.path.basename(path))
                )
                if target is None or target in analyzed:
                    continue
                _plex_command("PUT", f"/library/metadata/{rating_key}/analyze")
                analyzed.add(target)
                current_app.logger.info(
                    f"Plex analyze: {rating_key} '{os.path.basename(target)}' "
                    f"re-analyzed in '{section.get('title')}'"
                )
        start += len(page)
        if not page or start >= container.get("totalSize", 0):
            return
        if analyzed == targets:
            return


def analyze_plex_media(file_paths, retries=0):
    """Make Plex do its media analysis again for these library files.

    This is a queue task. It is the generic answer to "this file was
    rewritten in place". The item exists and its path is unchanged.
    Thus, nothing tells Plex to look again until its next scan, and the
    deep pass waits for the overnight maintenance. This task asks for
    both now.

    The task is best-effort by design, like every other Plex task here.
    It returns True in all cases, because the rewrite before it has
    already succeeded and committed. See the module docstring for what
    an analyze does and does not correct.
    """

    with app.app_context():
        if not (
            current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
        ):
            return True

        if isinstance(file_paths, str):
            file_paths = [file_paths]
        wanted = {os.path.abspath(path) for path in file_paths or [] if path}
        if not wanted:
            return True

        # Guard the FILE, not the section. This is the analysis of one
        # item. The important condition is that the copy on disk is
        # readable. An analyze against a dropped mount makes Plex record
        # the analysis of a file that it could not read. The section-wide
        # guard belongs to the refresh. There, a dead mount plus
        # emptyTrash deletes a library. That guard would also be the
        # wrong test here. A Plex that reaches the library by its own
        # mount paths declares locations that this host cannot see. The
        # basename match below exists exactly for that case.

        readable = set()
        for path in sorted(wanted):
            if not volume_alive(_probe_target(path)):
                current_app.logger.warning(
                    f"Plex analyze: '{os.path.basename(path)}' is on a dead or "
                    f"hung mount — not analyzing it now"
                )
            elif not os.path.exists(path):
                current_app.logger.warning(
                    f"Plex analyze: '{os.path.basename(path)}' is no longer on "
                    f"disk — not analyzing it now"
                )
            else:
                readable.add(path)

        # The basenames of the library carry the title, year, edition,
        # and quality. Thus, a basename identifies a file on its own.
        # plex_titles uses the same match. It is the fallback for a Plex
        # that reaches the library by a different mount path.

        by_basename = {os.path.basename(path): path for path in readable}

        try:
            payload = _plex_get("/library/sections")
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return True

        sections = [
            section
            for section in payload.get("MediaContainer", {}).get("Directory", []) or []
            if section.get("type") in SECTION_ITEM_TYPE
        ]

        analyzed = set()
        candidates = _sections_holding(sections, readable) if readable else []
        for section in candidates:
            try:
                _analyze_section(section, readable, by_basename, analyzed)
            except Exception:
                current_app.logger.warning(traceback.format_exc())
            if analyzed == readable:
                break

        unmatched = sorted(os.path.basename(path) for path in wanted - analyzed)
        if not unmatched:
            return True

        # The files that remain: a file that Plex has not scanned since
        # it appeared (the usual case, and a first scan analyzes it
        # anyway), or a file whose mount was down. The quarter-hourly
        # scan resolves both. Thus, one deferred attempt follows that
        # scan. Then the task drops the matter. It does not retry
        # forever.

        if retries < ANALYZE_MAX_RETRIES:
            current_app.logger.info(
                f"Plex analyze: nothing analyzed for {unmatched} — retrying "
                f"in {ANALYZE_RETRY_MINUTES} minutes"
            )
            current_app.maintenance_queue.enqueue_in(
                timedelta(minutes=ANALYZE_RETRY_MINUTES),
                "app.plex_library.analyze_plex_media",
                sorted(wanted - analyzed),
                retries=retries + 1,
                job_timeout=1800,
                # The key is the whole batch, not its first basename
                # (#242). Two different batches with the same first file
                # must not collapse into one retry.
                job_id=retry_job_id(
                    "analyze_plex_media",
                    hashlib.sha256("|".join(unmatched).encode()).hexdigest()[:16],
                    retries + 1,
                ),
                result_ttl=86400,
                description=f"Re-analyzing {len(unmatched)} file(s) in Plex",
            )
        else:
            current_app.logger.warning(
                f"Plex analyze: gave up on {unmatched} — still unreadable, or "
                f"no Plex item holds them"
            )
        return True


def enqueue_plex_analyze(file_path):
    """Queue a new analyze for a library file that was rewritten in place.

    Every in-place rewrite makes this call after its result is committed.
    This function never raises and never blocks the caller. The file is
    already correct on disk and in the database. Thus, an unreachable
    Plex or a refused enqueue costs only a stale Plex analysis, not the
    rewrite.
    """

    try:
        if not (
            current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
        ):
            return False
        basename = os.path.basename(file_path)
        job_id = safe_job_id(f"plex_analyze:{basename}")
        queue = current_app.maintenance_queue

        # A second edit while the first analyze is still queued needs no
        # second job. The queued job reads the file when it runs. A job
        # that already RUNS is not a duplicate. It may have read the file
        # before this edit arrived.

        if job_id in queue.job_ids:
            return False
        queue.enqueue(
            "app.plex_library.analyze_plex_media",
            args=([file_path],),
            job_timeout=1800,
            job_id=job_id,
            description=f"Re-analyzing '{basename}' in Plex",
        )
        return True
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return False
