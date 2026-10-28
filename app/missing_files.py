"""Find the best-quality files that have no local copy (#274).

A library file is the best copy of its title when it has rank 1 in the
quality ranking of the application. The ranking is movie_file_rank()
for the movies. For the TV episodes, it is the best holder of each
episode number (episode_rankings). Fitzflix
deletes a local copy when the archive at AWS is complete and the disk
is full, or when a person deletes it. Thus, a best copy can be absent
from the library disk. Then the title plays only after a restore from
the archive.

The scan makes one stat call for each best file. That is about 20,000
calls over the NFS mount, or 6 seconds. That is too slow for a page
render, and a stalled mount would stall the page with it. Thus, a
nightly task runs the scan and stores the result in Redis. The
maintenance pages read the stored result. A button on the page runs
the scan again on demand.

A missing mount makes every file look absent. The scan refuses to run
while a monitored volume does not respond. The stored result of the
previous run stays in place. A volume can also drop during the scan
(#396). Thus, the scan checks the volumes again at the first absent
file and after the last stat. It also compares the device id of each
library root before and after the stats. A remount gives the root a
new device id. A changed id or a dead volume abandons the result.
"""

import json
import os
from datetime import datetime, timezone

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.maintenance import missing_volumes
from app.file_ranking import episode_rankings, tv_rank_rows
from app.models import (
    File,
    Movie,
    RefQuality,
    movie_file_rank,
)

# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, the task can run on a worker without a second application

app = LocalProxy(get_app)

MISSING_KEY = "fitzflix:missing-best"


def ranked_file_rows():
    """Return (file_id, group_key, rank) for every movie and TV file.

    The group key names the title group of the ranking. Two files with
    the same group key compete for rank 1. The TV group key is 1 episode
    number. It does not include the edition.
    """

    movie_rows = (
        db.session.query(
            File.id,
            Movie.id,
            File.feature_type_id,
            File.plex_title,
            File.edition,
            movie_file_rank(),
        )
        .join(Movie, Movie.id == File.movie_id)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .all()
    )
    rows = [
        (file_id, ("movie", movie_id, feature, plex_title, edition), rank)
        for file_id, movie_id, feature, plex_title, edition, rank in movie_rows
    ]

    # Each episode number of a TV span is its own group (#479). A span
    # competes with the singles of each episode that it holds. A file
    # can thus be in more than 1 group.

    tv_rows = tv_rank_rows()
    for (series_id, season, number), members in episode_rankings(
        (row, row) for row in tv_rows
    ).items():
        rows.extend(
            (row.id, ("tv", series_id, season, number), rank)
            for rank, (row, _) in enumerate(members, start=1)
        )
    return rows


def _library_devices(config):
    """Return the device id of each library root, or None for a root that has no stat."""

    devices = {}
    for key in ("MOVIE_LIBRARY", "TV_LIBRARY"):
        try:
            devices[key] = os.stat(config[key]).st_dev
        except OSError:
            devices[key] = None
    return devices


def _volumes_dropped(config, devices_before):
    """Return a reason text if a volume dropped after the scan started, or None."""

    dead = missing_volumes(config)
    if dead:
        return f"{', '.join(dead)} not mounted"
    if _library_devices(config) != devices_before:
        return "a library volume was mounted again"
    return None


def scan_missing_best_files():
    """Stat every best file and return the scan result, or None.

    The result is None when a monitored volume does not respond. Then
    nothing is known about the files, and the caller keeps the previous
    result. Otherwise the result has the ids of the absent best files.
    It also has the quality title of the best present copy in the
    same title group, when there is one.
    """

    dead = missing_volumes(current_app.config)
    if dead:
        current_app.logger.warning(
            f"Skipping the missing-file scan: {', '.join(dead)} not mounted"
        )
        return None
    devices_before = _library_devices(current_app.config)

    library_dir = current_app.config["LIBRARY_DIR"]
    rows = ranked_file_rows()
    paths = dict(
        db.session.query(File.id, File.file_path)
        .filter(File.id.in_([file_id for file_id, _, _ in rows]))
        .all()
    )
    quality_titles = dict(
        db.session.query(File.id, RefQuality.quality_title)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.id.in_(list(paths)))
        .all()
    )

    # One stat call per best file. The siblings of an absent best file
    # are checked only when the best file is absent. Thus, a complete
    # library costs one stat per title.

    present = {}

    def is_present(file_id):
        if file_id not in present:
            present[file_id] = os.path.isfile(os.path.join(library_dir, paths[file_id]))
        return present[file_id]

    groups = {}
    for file_id, group_key, rank in rows:
        groups.setdefault(group_key, []).append((rank, file_id))

    missing = []
    fallbacks = {}
    for members in groups.values():
        members.sort()
        best_rank, best_id = members[0]
        if is_present(best_id):
            continue

        # The first absent file can be the first sign of a dropped
        # volume. Then all later files also look absent.

        if not missing:
            reason = _volumes_dropped(current_app.config, devices_before)
            if reason:
                current_app.logger.warning(
                    f"Abandoning the missing-file scan: {reason} during the scan"
                )
                return None
        if best_id in missing:
            continue
        missing.append(best_id)
        for rank, file_id in members[1:]:
            if is_present(file_id):
                fallbacks[best_id] = quality_titles[file_id]
                break

    reason = _volumes_dropped(current_app.config, devices_before)
    if reason:
        current_app.logger.warning(
            f"Abandoning the missing-file scan: {reason} during the scan"
        )
        return None

    return {
        "checked": datetime.now(timezone.utc).isoformat(),
        "ranked": len(groups),
        "missing": sorted(missing),
        "fallbacks": {str(file_id): title for file_id, title in fallbacks.items()},
    }


def missing_best_files_task():
    """Run the scan and store its result in Redis (the nightly task)."""

    with app.app_context():
        result = scan_missing_best_files()
        if result is None:
            return
        current_app.redis.set(MISSING_KEY, json.dumps(result))
        count = len(result["missing"])
        current_app.logger.info(
            f"Missing-file scan: {count} of {result['ranked']} best files "
            f"have no local copy"
        )


def stored_scan(redis):
    """Return the stored scan result, or None before the first scan."""

    raw = redis.get(MISSING_KEY)
    if not raw:
        return None
    result = json.loads(raw)
    result["checked"] = datetime.fromisoformat(result["checked"])
    return result


def missing_best_summary(redis):
    """Return the count and the scan time for a badge, or None before a scan.

    The count includes only the files that are still in the database. An
    import that replaces a file deletes its row. Thus, the badge agrees
    with the page until the next scan.
    """

    result = stored_scan(redis)
    if result is None:
        return None
    count = (
        File.query.filter(File.id.in_(result["missing"])).count()
        if result["missing"]
        else 0
    )
    return {"count": count, "checked": result["checked"]}


def missing_best_report(redis):
    """Return the stored scan as rows for the page, with the files loaded.

    A file deleted from the database after the scan is not in the rows.
    The rows are in title order, with the movies before the episodes.
    """

    result = stored_scan(redis)
    if result is None:
        return None

    files = (
        File.query.filter(File.id.in_(result["missing"])).all()
        if result["missing"]
        else []
    )
    rows = []
    for file in files:
        if file.movie_id:
            title = f"{file.movie.title} ({file.movie.year})"
            sort_key = (
                0,
                file.movie.title or "",
                file.movie.year or 0,
                file.plex_title,
            )
        else:
            title = file.tv_series.title
            sort_key = (1, title, file.season or 0, file.episode or 0)
        rows.append(
            {
                "file": file,
                "title": title,
                "quality": file.quality.quality_title,
                "fallback": result["fallbacks"].get(str(file.id)),
                "archived": bool(file.aws_key),
                "sort_key": sort_key,
            }
        )
    rows.sort(key=lambda row: row["sort_key"])
    return {
        "checked": result["checked"],
        "ranked": result["ranked"],
        "rows": rows,
    }
