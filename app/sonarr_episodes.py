"""Sonarr-sourced episode data for Sonarr-managed series (#162).

TMDB is the series-level metadata source (cast, crew, posters,
production info), but its TV episode numbering diverges from the TVDB
aired order Sonarr used to number the library's files — which is why
tv_validation suppresses episode titles on numbering-suspect series,
and why TVDB-specialty shows (The Match Game's S09-S15, Carson) show
placeholders or nothing at all.

Sonarr itself is the fix: it keeps TVDB's full episode metadata for
every series it manages, refreshed on its own ~12-hour cadence, and
its numbering matches the library's files by construction. The
nightly sync here reads that local API (never TheTVDB — no key or
subscriber PIN involved) and makes Sonarr the episode source of
record for every series it manages: tv_episode rows are rebuilt from
Sonarr's listing and the series is stamped episode_source="sonarr",
which tells tmdb_tv_refresh to leave those rows alone and the render
and search gates to trust the titles even on numbering-suspect
series. Series Sonarr doesn't manage keep episode_source="tmdb" and
the existing TMDB pipeline.

Series are matched to Sonarr by LIBRARY FOLDER first — the basename
of Sonarr's series path against the Fitzflix series title, which is
the folder name by construction. The folder is ground truth: it is
the very directory Sonarr imports into, whatever TVDB entry it files
the show under, and TMDB's tvdb external id can point at a duplicate
TVDB entry Sonarr doesn't use (Popeye, Fullmetal Alchemist). The
TVDB id is the fallback for the few titles whose folder name isn't
the title verbatim (trailing dots — "The Venture Bros.").

Safety rails: a failed or empty Sonarr fetch keeps a series' stored
rows rather than wiping titles over a transient glitch (#251); a
series is only flipped to Sonarr when the fetch yields real titles
(TVDB titles its British "Episode N" shows just like TMDB does) AND
the entry's numbering describes at least MIN_COVERAGE of the series'
non-edition file slots — a folder can match a near-empty or
differently-numbered TVDB entry (You're Under Arrest's 4-episode OVA
listing vs 32 files) whose adoption would mislabel; and a
Sonarr-sourced series that disappears from Sonarr or fails the
coverage guard flips back to TMDB with its rows dropped and a TMDB
refresh queued, so the guide rebuilds in TMDB numbering instead of
mislabeling TVDB-numbered leftovers.
"""

import json
import os
import re
import traceback

from datetime import datetime, timezone

import urllib3

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File, TVEpisode, TVSeries

app = LocalProxy(get_app)

# TMDB's and TVDB's slot-shaped non-titles: a "title" carrying nothing
# the episode number doesn't already say

PLACEHOLDER_RE = re.compile(r"^(episode \d+|season \d+, episode \d+|tba)$", re.I)

# A matched Sonarr entry must actually describe the files before its
# episodes replace the stored guide: below this fraction of the series'
# file slots covered, the entry is the wrong one (a folder matched to a
# near-empty duplicate TVDB listing) and whatever the series has stands

MIN_COVERAGE = 0.5


def title_is_placeholder(title):
    """True when an episode title carries no information: absent, blank,
    or a slot-shaped filler like "Episode 12" or "TBA"."""

    return not title or bool(PLACEHOLDER_RE.match(title.strip()))


def _sonarr_get(path):
    """GET a Sonarr v3 endpoint, returning parsed JSON or None on any
    failure. Uses urllib3 like the other arr helpers — requests
    segfaults on this host for arr calls."""

    try:
        http = urllib3.PoolManager()
        r = http.request(
            "GET",
            current_app.config["SONARR_URL"] + path,
            headers={"X-Api-Key": current_app.config["SONARR_API_KEY"]},
            # Bounded, so a wedged Sonarr can't hang the worker
            timeout=urllib3.Timeout(connect=5, read=60),
            retries=False,
        )
        if r.status != 200:
            current_app.logger.warning(
                f"Sonarr episodes: GET {path} answered HTTP {r.status}"
            )
            return None
        return json.loads(r.data.decode("utf-8"))
    except Exception as e:
        current_app.logger.warning(f"Sonarr episodes: GET {path} failed: {e}")
        return None


def _file_slots():
    """{series_id: {(season, episode)}} for every episode slot the
    library's files occupy, multi-episode spans expanded — the
    denominator for the coverage guard. Edition-carrying files are
    excluded: they're often custom-numbered (Doctor Who's S00E9001
    extras) and self-titled, so no provider's numbering could or need
    describe them."""

    slots = {}
    rows = (
        db.session.query(File.series_id, File.season, File.episode, File.last_episode)
        .filter(File.series_id.isnot(None))
        .filter(File.season.isnot(None), File.episode.isnot(None))
        .filter(db.or_(File.edition.is_(None), File.edition == ""))
    )
    for series_id, season, episode, last_episode in rows:
        for number in range(episode, (last_episode or episode) + 1):
            slots.setdefault(series_id, set()).add((season, number))
    return slots


def _usable_slots(payload):
    """{(season, episode): episode dict} for every genuinely-titled
    episode in a Sonarr listing — placeholder titles are dropped, so a
    series TVDB itself titles "Episode N" yields nothing."""

    slots = {}
    for episode in payload or []:
        season = episode.get("seasonNumber")
        number = episode.get("episodeNumber")
        title = (episode.get("title") or "").strip()
        if season is None or number is None or title_is_placeholder(title):
            continue
        slots[(season, number)] = episode
    return slots


def _all_slots(payload):
    """{(season, episode)} for every episode in a Sonarr listing,
    placeholders included — the coverage guard's numerator. Coverage
    asks whether the entry's NUMBERING describes the files; a
    placeholder-titled slot still describes its file (Top Gear's
    "Episode N" seasons), it just contributes no title."""

    slots = set()
    for episode in payload or []:
        season = episode.get("seasonNumber")
        number = episode.get("episodeNumber")
        if season is not None and number is not None:
            slots.add((season, number))
    return slots


def _sync_series_rows(series, slots):
    """Rebuild one series' tv_episode rows from Sonarr slots: upsert
    every slot, drop rows Sonarr no longer lists (including TMDB
    leftovers from before the flip), and stamp the series
    Sonarr-sourced. Returns the number of rows synced."""

    now = datetime.now(timezone.utc)
    existing = {(row.season, row.episode): row for row in series.episodes.all()}

    for (season, number), episode in slots.items():
        row = existing.get((season, number))
        if row is None:
            row = TVEpisode(season=season, episode=number)
            series.episodes.append(row)
        title = episode["title"].strip()
        row.title = title[:256]
        row.overview = episode.get("overview") or None
        row.air_date = (
            datetime.strptime(episode["airDate"], "%Y-%m-%d")
            if episode.get("airDate")
            else None
        )
        row.runtime = episode.get("runtime")
        row.tmdb_episode_id = None
        row.tmdb_still_path = None
        row.tmdb_data_as_of = now

    for slot, row in existing.items():
        if slot not in slots:
            db.session.delete(row)

    series.episode_source = "sonarr"
    return len(slots)


def _revert_to_tmdb(series):
    """Flip a Sonarr-sourced series back to TMDB: its rows follow
    TVDB numbering, which would mislabel under TMDB's, so they're
    dropped — and a TMDB refresh is queued to repopulate the guide,
    since the nightly change-sweep only touches TMDB-changed records
    and would otherwise leave the series bare indefinitely."""

    for row in series.episodes.all():
        db.session.delete(row)
    series.episode_source = "tmdb"

    if series.tmdb_id:
        current_app.sql_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("TV Shows", series.id, series.tmdb_id),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=f"Refreshing TMDB data for '{series.title}'",
        )


def sync_sonarr_episodes():
    """Task: make Sonarr the episode source of record for every series
    it manages, rebuilding their tv_episode rows from its local TVDB
    metadata. No-ops without Sonarr configuration; each series commits
    on its own, so a mid-run failure keeps the finished ones."""

    with app.app_context():
        if not (
            current_app.config.get("SONARR_URL")
            and current_app.config.get("SONARR_API_KEY")
        ):
            return True

        try:
            listing = _sonarr_get("/api/v3/series")
            if listing is None:
                current_app.logger.warning(
                    "Sonarr episodes: series list unavailable, keeping "
                    "everything as it stands"
                )
                return True
            by_folder = {
                os.path.basename(entry["path"]): entry
                for entry in listing
                if entry.get("path")
            }
            by_tvdb = {
                entry.get("tvdbId"): entry for entry in listing if entry.get("tvdbId")
            }
            file_slots = _file_slots()

            synced, episodes = 0, 0
            empty, failed, reverted, uncovered = [], [], [], []
            for series in TVSeries.query.all():
                sonarr = by_folder.get(series.title) or (
                    by_tvdb.get(series.tvdb_id) if series.tvdb_id else None
                )

                if sonarr is None:
                    if series.episode_source == "sonarr":
                        _revert_to_tmdb(series)
                        reverted.append(series.title)
                        db.session.commit()
                    continue

                payload = _sonarr_get(f"/api/v3/episode?seriesId={sonarr['id']}")
                if payload is None:
                    failed.append(series.title)
                    continue

                # The entry must describe the files before its episodes
                # replace the stored guide: a matched folder can carry a
                # near-empty or differently-numbered TVDB listing
                # (You're Under Arrest's 4-episode OVA entry; The
                # State's 59-episodes-in-one-season order vs the files'
                # four seasons). An already-flipped series failing this
                # is mislabeling right now — revert it to TMDB

                owned = file_slots.get(series.id, set())
                if owned:
                    described = _all_slots(payload)
                    covered = sum(1 for slot in owned if slot in described) / len(owned)
                    if covered < MIN_COVERAGE:
                        if series.episode_source == "sonarr":
                            _revert_to_tmdb(series)
                            db.session.commit()
                        uncovered.append(series.title)
                        continue

                slots = _usable_slots(payload)
                if not slots:

                    # Nothing usable — a series TVDB itself only titles
                    # "Episode N" (The Hour), or a glitched listing.
                    # Whatever the series has stands: TMDB rows aren't
                    # worth trading for nothing, and existing Sonarr
                    # rows aren't worth wiping (#251)

                    empty.append(series.title)
                    continue

                episodes += _sync_series_rows(series, slots)
                synced += 1
                db.session.commit()

            current_app.logger.info(
                f"Sonarr episodes: {synced} series synced "
                f"({episodes} episodes), {len(reverted)} reverted to TMDB "
                f"({', '.join(reverted) or 'none'}), {len(empty)} without "
                f"usable titles ({', '.join(empty) or 'none'}), "
                f"{len(uncovered)} matched entries covering too few file "
                f"slots ({', '.join(uncovered) or 'none'}), "
                f"{len(failed)} fetches failed ({', '.join(failed) or 'none'})"
            )
        except Exception:
            db.session.rollback()
            current_app.logger.error(traceback.format_exc())
            return False

        return True
