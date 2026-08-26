"""Plex library refresh + trash emptying, safely.

Replaces the external cron that curl'd refresh and emptyTrash for
hardcoded section ids, guarded by checking one mount per section. The
guard is the whole point: if a section's directory is missing (an SMB
mount dropped), a scan marks everything missing and emptying the
trash then deletes the library's metadata — watch states included —
rebuilding it from scratch when the mount returns.

This version asks Plex for each movie/show section's OWN location
paths and requires every location's MOUNT to be alive before touching
that section (the old script checked a single mount per section — the
Movies section actually has two roots on two volumes). The guard is
mount-level, not leaf-level: an empty transcodes volume legitimately
lacks its Movies subfolder, and an absent leaf on a healthy mount is
harmless to scan. Mount probes go through volume_alive, which treats
a hung SMB stat as dead instead of hanging the worker.

It also owns the generic re-analyze: any task that rewrites a library
file IN PLACE calls enqueue_plex_analyze, so Plex re-reads the file at
once instead of at the pace of its own scan and overnight analysis
pass. Plex's manual analyze re-reads the streams and regenerates
chapter thumbs — about two seconds on a 45 GB film (#194).

What it does NOT touch is the Media-level audioCodec/audioChannels
summary Plex shows as an item's audio. Measured Aug 24 2026 over 3,153
films, that summary is the file's HIGHEST-CHANNEL audio track, matching
it 100% of the time and the first/default track only 94.6% (i.e. only
when they coincide). A supplemented film therefore reads "FLAC 7.1" for
as long as its lossless twin outranks the DD+ Atmos 5.1, and analyzing
it — by this task or by hand in Plex — will not change that.
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

# What to page for in each kind of section: a movie section's own
# items carry the files, but in a show section they hang off episodes

SECTION_ITEM_TYPE = {"movie": 1, "show": 4}

# A file Plex hasn't scanned yet can't be analyzed. Rather than lose
# the analyze, one deferred attempt follows the next quarter-hourly
# library scan

ANALYZE_RETRY_MINUTES = 20
ANALYZE_MAX_RETRIES = 1


def _probe_target(path):
    """What to health-check for a section location: the /Volumes mount
    that backs it, or the path itself when it isn't volume-backed."""

    if path.startswith(VOLUMES_ROOT + os.sep):
        return "/".join(path.split("/")[:3])
    return path


def _section_locations(section):
    """The paths a section declares as its roots."""

    return [
        location.get("path")
        for location in section.get("Location", []) or []
        if location.get("path")
    ]


def _part_files(item):
    """Every part file path an item's media declares."""

    for media in item.get("Media", []) or []:
        for part in media.get("Part", []) or []:
            if part.get("file"):
                yield part["file"]


def _plex_command(method, path):
    """A command-style Plex call (refresh, emptyTrash): these answer
    with an EMPTY body, so nothing is parsed — success is the status."""

    r = requests.request(
        method,
        current_app.config["PLEX_URL"] + path,
        params={"X-Plex-Token": current_app.config["PLEX_TOKEN"]},
        timeout=60,
    )
    r.raise_for_status()


def refresh_plex_libraries():
    """Task: scan each movie/show section for changes and empty its
    trash — per section, and only when every location it declares is
    mounted and present."""

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

            # A dead or hung mount makes scanning dangerous — skip the
            # whole section until it's back

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
    """The sections that could hold these files: one whose declared
    location is a parent of a wanted path owns it.

    Narrowing this way keeps a movie's analyze from paging the whole TV
    section. When nothing matches — Plex mounting the library at paths
    we don't share — every movie/show section is walked instead, since
    the part-file basenames can still identify the items.
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
    """Analyze every target file this section holds, adding each one it
    matched to `analyzed`. Files are matched on their full path, or on
    their basename when Plex knows the library by another path."""

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
    """Task: have Plex redo its media analysis for these library files.

    The generic answer to "this file was rewritten in place": the item
    exists and its path is unchanged, so nothing tells Plex to look
    again until its next scan, and the deep pass waits for overnight
    maintenance. This asks for both now.

    Best-effort by design, like every other Plex task here: it answers
    True whatever happened, because the rewrite it follows has already
    succeeded and committed. See the module docstring for what an
    analyze does and does not correct.
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

        # Guard the FILE, not the section: this is one item's analysis,
        # and what matters is that the copy on disk is readable —
        # analyzing against a dropped mount has Plex record the analysis
        # of a file it couldn't read. (The section-wide guard belongs to
        # the refresh, where a dead mount plus emptyTrash wipes a
        # library. It would also be the wrong test here: a Plex that
        # reaches the library by its own mount paths declares locations
        # this host can't see — exactly the case the basename match
        # below exists to serve.)

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

        # The library's basenames carry title, year, edition and
        # quality, so they identify a file on their own — the same
        # match plex_titles relies on, kept as the fallback for a Plex
        # that reaches the library by a different mount path

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

        # Left over: a file Plex hasn't scanned since it appeared (the
        # ordinary case — a first scan analyzes it anyway), or one whose
        # mount was down. Either resolves within the quarter-hourly
        # scan, so one deferred attempt follows it and then the matter
        # is dropped rather than retried forever

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
                # Keyed on the whole batch, not its first basename (#242):
                # two distinct batches sharing a first file must not
                # dedupe into one retry
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
    """Queue a re-analyze for a library file that was rewritten in place.

    The call every in-place rewrite makes once its result is committed.
    Never raises and never blocks the caller: the file is already
    correct on disk and in the database, so an unreachable Plex or a
    refused enqueue costs a stale Plex analysis, not the rewrite.
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
        # second job — the queued one reads the file when it runs. One
        # already RUNNING is not deduped against: it may have read the
        # file before this edit landed

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
