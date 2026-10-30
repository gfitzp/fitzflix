"""Finish the downloads that Sonarr and Radarr cannot import (#276).

SABnzbd completes a download. Then Radarr or Sonarr refuses to import
it. One cause is a release that matched the movie by id only. An other
cause is a file that does not match an episode. The app marks the
queue item "manual import required" and waits. Before this module, a
person had to log in to the server and find the download. Then the
person had to rename it, copy it to the import directory, and delete
the original.

Now the apps send a ManualInteractionRequired webhook event to the
import endpoints. Fitzflix records the download in Redis and emails
the admin once. The Manual imports page reads the live queues of both
apps. It lists each blocked download with the video files in its
folder and a proposed Fitzflix filename. One button copies the chosen
file to the import directory under that name. Then it deletes the
download folder and removes the queue item from the app. The
filesystem watcher then imports the file as usual.

Fitzflix touches only the files below ARR_DOWNLOAD_FOLDERS. That is
the same fence as the root folders of the import webhooks.
"""

import json
import os
import re
import shutil
from datetime import datetime, timezone

import urllib3
from flask import current_app, url_for
from werkzeug.local import LocalProxy

from app import get_app, safe_job_id
from app.api.arr import downgrade_quality_title
from app import naming
from app.models import MOVIES, TV_SHOWS
from app.tasks import LIVE_JOB_STATES, TASKS, resolve_queue

# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, the task can run on a worker without a second application

app = LocalProxy(get_app)

PENDING_KEY = "fitzflix:manual-imports"

# The queue states of Sonarr and Radarr that wait for a person

BLOCKED_STATES = ("importBlocked", "importPending", "importFailed")

VIDEO_EXTENSIONS = (
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".ts",
    ".m2ts",
    ".mpg",
    ".mpeg",
    ".wmv",
    ".webm",
)


def arr_settings(service):
    """Return the base URL and the API key of a service, or (None, None)."""

    base_url = current_app.config.get(f"{service.upper()}_URL")
    api_key = current_app.config.get(f"{service.upper()}_API_KEY")
    if not base_url or not api_key:
        return None, None
    return base_url.rstrip("/"), api_key


def arr_request(service, method, path, fields=None):
    """Send one request to a service API and return the decoded body.

    The timeout is bounded. Thus, a stalled app cannot hang a page
    render or a worker. A failure raises. The callers decide what a
    failure means to them."""

    base_url, api_key = arr_settings(service)
    if base_url is None:
        raise RuntimeError(f"{service} is not configured")
    http = urllib3.PoolManager()
    response = http.request(
        method,
        f"{base_url}/api/v3/{path}",
        fields=fields,
        headers={"X-Api-Key": api_key},
        timeout=urllib3.Timeout(connect=5, read=30),
        retries=False,
    )
    if not 200 <= response.status < 300:
        raise RuntimeError(f"{service} {path} returned HTTP {response.status}")
    if not response.data:
        return None
    return json.loads(response.data.decode("utf-8"))


def fetch_queue(service):
    """Return every queue item of a service with its movie or episode."""

    fields = {"includeMovie": "true"}
    if service == "Sonarr":
        fields = {"includeSeries": "true", "includeEpisode": "true"}
    return arr_request(service, "GET", "queue/details", fields) or []


def blocked_downloads(service, queue):
    """Group the queue items that wait for a person by download.

    Sonarr makes one queue item for each episode of a download. Thus,
    the items of one download collapse into one entry here. The
    entry keeps every queue id. The removal needs all of them."""

    entries = {}
    for item in queue:
        if item.get("trackedDownloadState") not in BLOCKED_STATES:
            continue
        download_id = item.get("downloadId")
        if not download_id:
            continue
        entry = entries.get(download_id)
        if entry is None:
            entry = entries[download_id] = {
                "service": service,
                "download_id": download_id,
                "queue_ids": [],
                "title": item.get("title"),
                "output_path": item.get("outputPath"),
                "size": item.get("size") or 0,
                "quality": ((item.get("quality") or {}).get("quality") or {}).get(
                    "name"
                ),
                "score": item.get("customFormatScore") or 0,
                "messages": [],
                "episodes": [],
                "record": None,
            }
        entry["queue_ids"].append(item.get("id"))
        for status in item.get("statusMessages") or []:
            for message in status.get("messages") or []:
                if message not in entry["messages"]:
                    entry["messages"].append(message)
        episode = item.get("episode")
        if episode:
            entry["episodes"].append(
                (episode.get("seasonNumber"), episode.get("episodeNumber"))
            )
        if entry["record"] is None:
            entry["record"] = matched_record(service, item)
    return list(entries.values())


def matched_record(service, item):
    """Describe the movie or series that the app matched the download to."""

    if service == "Radarr":
        movie = item.get("movie") or {}
        if not movie:
            return None
        return {
            "kind": "movie",
            "title": movie.get("title"),
            "year": movie.get("year"),
            "tmdb_id": movie.get("tmdbId"),
        }
    series = item.get("series") or {}
    if not series:
        return None
    return {
        "kind": "series",
        "title": series.get("title"),
        "year": series.get("year"),
        "tmdb_id": series.get("tmdbId"),
    }


def download_folders():
    """Return the real paths of the download folders that Fitzflix may touch."""

    return [
        os.path.realpath(folder)
        for folder in current_app.config.get("ARR_DOWNLOAD_FOLDERS") or []
    ]


def fenced_path(path):
    """Return the real path if it is below a download folder, or None.

    The folder itself is not below itself. Thus, Fitzflix can never
    delete a download folder root."""

    if not path:
        return None
    real = os.path.realpath(path)
    for folder in download_folders():
        if os.path.commonpath([real, folder]) == folder and real != folder:
            return real
    return None


def video_candidates(output_path):
    """List the video files of a download folder, largest first.

    Each candidate has the path relative to the folder and its size.
    A hidden file and an AppleDouble file are not candidates. A
    download that is one file instead of a folder is its own only
    candidate."""

    real = fenced_path(output_path)
    if real is None or not os.path.exists(real):
        return []
    if os.path.isfile(real):
        if real.lower().endswith(VIDEO_EXTENSIONS):
            return [{"relative": os.path.basename(real), "size": os.path.getsize(real)}]
        return []
    found = []
    for root, _dirs, files in os.walk(real):
        for name in files:
            if name.startswith(".") or not name.lower().endswith(VIDEO_EXTENSIONS):
                continue
            full = os.path.join(root, name)
            found.append(
                {
                    "relative": os.path.relpath(full, real),
                    "size": os.path.getsize(full),
                }
            )
    found.sort(key=lambda candidate: candidate["size"], reverse=True)
    return found


def proposed_name(entry, extension):
    """Build the Fitzflix filename for a blocked download.

    A movie becomes "Title (Year) - [Quality].ext". An episode becomes
    "Series - S01E02 - [Quality].ext". The quality is the downgraded
    web title of the import webhooks. The release name is the fallback
    when the app matched nothing."""

    from app.importing import sanitize_string

    from app.settings import get_setting

    quality = "WEBDL-1080p"
    if entry.get("quality"):
        service = "SONARR" if entry.get("service") == "Sonarr" else "RADARR"
        quality = downgrade_quality_title(
            entry["quality"],
            entry.get("score") or 0,
            get_setting(f"{service}_WEBDL_MIN_SCORE"),
        )
    extension = (extension or "mkv").lstrip(".")
    record = entry.get("record")
    if not record or not record.get("title"):
        return f"{sanitize_string(entry.get('title') or 'Unknown')} - [{quality}].{extension}"
    parts = {
        "title": sanitize_string(record["title"]),
        "quality_title": quality,
        "extension": extension,
    }
    if record["kind"] == "movie":
        parts.update(library=MOVIES, year=record.get("year") or "0000")
        return naming.library_name(parts)[1]
    episodes = sorted(
        (season, number)
        for season, number in entry.get("episodes") or []
        if season is not None and number is not None
    ) or [(0, 0)]
    parts.update(library=TV_SHOWS, season=episodes[0][0], episode=episodes[0][1])
    if len(episodes) > 1:
        parts["last_episode"] = episodes[-1][1]
    return naming.library_name(parts)[1]


def fitzflix_basename(name):
    """Return True if a name is a safe basename in a Fitzflix filename form.

    The import pipeline is the final judge. This check only stops a
    name with a path separator, a hidden name, and a name that the
    pipeline would reject at once."""

    if not name or name.startswith(".") or "/" in name or "\\" in name:
        return False
    movie = r".+ \(\d{4}\)(?: .+)? \- (?:.+ )?\[.+\]\.\w+"
    episode = r".+ \- S\d+E\d+(?:\-E\d+)? \- (?:.+ )?\[.+\]\.\w+"
    return bool(re.fullmatch(movie, name) or re.fullmatch(episode, name))


def manual_import_job_id(download_id):
    """Return the fixed job id of the import task of a download."""

    return safe_job_id(f"manual-import:{download_id}")


def started(download_id):
    """Return True if an import task of this download waits or runs (#626).

    The job under the fixed id is the record. free_job_id reads the same
    job. Thus, the page and the refusal of a second import agree with it."""

    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    queue = resolve_queue(TASKS["app.manual_imports.manual_import_task"].queue)
    try:
        job = Job.fetch(manual_import_job_id(download_id), connection=queue.connection)
    except NoSuchJobError:
        return False
    return job.get_status(refresh=False) in LIVE_JOB_STATES


def pending_report():
    """Read the live queues and describe every blocked download.

    The result has the entries, with the candidates and the proposed
    name of each, and the services that did not answer. The stored
    Redis hash follows the live result. Thus, the badge count and the
    email record stay correct after a person solves a download in the
    app itself."""

    entries = []
    errors = []
    reachable = []
    for service in ("Radarr", "Sonarr"):
        base_url, _api_key = arr_settings(service)
        if base_url is None:
            continue
        try:
            queue = fetch_queue(service)
        except Exception as e:
            errors.append(f"{service} did not answer: {e}")
            continue
        reachable.append(service)
        entries.extend(blocked_downloads(service, queue))

    redis = current_app.redis
    for entry in entries:
        entry["fenced"] = fenced_path(entry["output_path"]) is not None
        entry["candidates"] = video_candidates(entry["output_path"])
        extension = "mkv"
        if entry["candidates"]:
            extension = os.path.splitext(entry["candidates"][0]["relative"])[1]
        entry["proposed_name"] = proposed_name(entry, extension)
        entry["started"] = started(entry["download_id"])
        entry["record_url"] = record_url(entry.get("record"))

    reconcile_pending(redis, entries, reachable)
    entries.sort(key=lambda entry: (entry["service"], entry["title"] or ""))
    return {"entries": entries, "errors": errors}


def record_url(record):
    """Return the Fitzflix page of the matched title, or None."""

    if not record or not record.get("tmdb_id"):
        return None
    from app.models import Movie, TVSeries

    if record["kind"] == "movie":
        movie = Movie.query.filter(Movie.tmdb_id == record["tmdb_id"]).first()
        if movie:
            return url_for("main.movie", movie_id=movie.id)
        return None
    series = TVSeries.query.filter(TVSeries.tmdb_id == record["tmdb_id"]).first()
    if series:
        return url_for("main.tv", series_id=series.id)
    return None


def reconcile_pending(redis, entries, reachable):
    """Make the stored hash equal to the live queues of the reachable apps."""

    live = {entry["download_id"]: entry for entry in entries}
    stored = {
        key.decode(): json.loads(raw) for key, raw in redis.hgetall(PENDING_KEY).items()
    }
    for download_id, record in stored.items():
        if download_id not in live and record.get("service") in reachable:
            redis.hdel(PENDING_KEY, download_id)
    for download_id, entry in live.items():
        if download_id not in stored:
            store_pending(redis, entry["service"], download_id, entry["title"])


def store_pending(redis, service, download_id, title):
    """Add a download to the stored hash. Return True if it was new."""

    record = json.dumps(
        {
            "service": service,
            "title": title,
            "received": datetime.now(timezone.utc).isoformat(),
        }
    )
    return bool(redis.hsetnx(PENDING_KEY, download_id, record))


def pending_count(redis):
    """Return the number of downloads that wait for a manual import."""

    return redis.hlen(PENDING_KEY)


def record_manual_interaction(service, payload):
    """Record a ManualInteractionRequired webhook event and email the admin.

    The event arrives once for each download. A second event for the
    same download, for example after a restart of the app, does not
    send a second email. Return True if the download was new."""

    download_id = payload.get("downloadId")
    title = (payload.get("downloadInfo") or {}).get("title") or (
        payload.get("release") or {}
    ).get("releaseTitle")
    if not download_id:
        current_app.logger.warning(
            f"{service} manual-interaction event without a download id; ignoring it"
        )
        return False
    messages = [
        message
        for status in payload.get("downloadStatusMessages") or []
        for message in status.get("messages") or []
    ]
    reason = "; ".join(messages) or "no reason given"
    if not still_blocked(service, download_id):
        current_app.logger.info(
            f"{service} reported '{title}' for manual import, but the download "
            f"no longer waits in its queue; ignoring the event"
        )
        return False
    if not store_pending(current_app.redis, service, download_id, title):
        current_app.logger.info(
            f"{service} reported '{title}' for manual import again; already recorded"
        )
        return False
    current_app.logger.warning(
        f"{service} cannot import '{title}' ({reason}); it waits on the "
        f"Manual imports page"
    )
    email_manual_import_needed(service, title, reason)
    return True


def still_blocked(service, download_id):
    """Return True if the download waits in the live queue of the app.

    The app can send the event after Fitzflix removed the queue item,
    for example 3 seconds after the import of "M" (2026-10-13). Then
    the event is stale. If the app does not answer, the event counts.
    A lost notice is worse than a duplicate."""

    try:
        queue = fetch_queue(service)
    except Exception as e:
        current_app.logger.warning(
            f"{service} did not answer the queue check of a manual-import "
            f"event: {e}"
        )
        return True
    return any(
        item.get("downloadId") == download_id
        and item.get("trackedDownloadState") in BLOCKED_STATES
        for item in queue
    )


def email_manual_import_needed(service, title, reason):
    """Email the admin about a download that waits for a manual import."""

    from markupsafe import escape

    from app.email import notify_admin_text

    if not current_app.config.get("MAIL_SERVER"):
        return
    page = url_for("main.manual_imports", _external=True)

    # The title and the reason come from the arr. The HTML part escapes
    # them (#450).

    notify_admin_text(
        f"{service} needs a manual import",
        f"{service} downloaded '{title}' but cannot import it.\n\n"
        f"Reason: {reason}\n\n"
        f"Finish the import at {page}",
        f"<p>{escape(service)} downloaded '{escape(title)}' but cannot "
        f"import it.</p>"
        f"<p>Reason: {escape(reason)}</p>"
        f'<p><a href="{escape(page)}">Finish the import on the Manual imports '
        f"page</a></p>",
    )


def remove_from_queue(service, download_id, delete_files):
    """Remove every queue item of a download from the app.

    With delete_files, the app tells the download client to delete the
    download too. Return the number of queue items removed."""

    queue = fetch_queue(service)
    removed = 0
    for item in queue:
        if item.get("downloadId") != download_id:
            continue
        arr_request(
            service,
            "DELETE",
            f"queue/{item['id']}",
            {
                "removeFromClient": "true" if delete_files else "false",
                "blocklist": "false",
                "skipRedownload": "true",
            },
        )
        removed += 1
    return removed


def manual_import_task(service, download_id, output_path, relative, new_basename):
    """Copy a blocked download into the import directory and clean up.

    The copy lands under a hidden name. The rename to the real name
    makes the filesystem event that the import watcher acts on. Only
    after a complete copy does the task delete the download folder and
    remove the queue items from the app. A failure leaves the download
    in place. Then the page shows the button again."""

    with app.app_context():
        redis = current_app.redis
        folder = fenced_path(output_path)

        # A download can be 1 file. Then the file is its own only
        # candidate (video_candidates), and the source is that file.

        if folder and os.path.isfile(folder):
            source = folder if os.path.basename(folder) == relative else None
        else:
            source = (
                fenced_path(os.path.join(output_path, relative)) if folder else None
            )
        import_dir = current_app.config["IMPORT_DIR"]
        hidden = naming.hidden(os.path.join(import_dir, new_basename))
        final = os.path.join(import_dir, new_basename)
        try:
            if source is None or not os.path.isfile(source):
                raise RuntimeError(
                    f"'{relative}' is not a file below the download folders"
                )
            if os.path.exists(final) or os.path.exists(hidden):
                raise RuntimeError(
                    f"'{new_basename}' already exists in the import directory"
                )
            current_app.logger.info(
                f"'{new_basename}' Copying '{source}' to the import directory"
            )
            shutil.copyfile(source, hidden)
            if os.path.getsize(hidden) != os.path.getsize(source):
                raise RuntimeError("the copy is a different size than the source")
            os.rename(hidden, final)
            current_app.logger.info(f"'{new_basename}' Copied to the import directory")

            if os.path.isdir(folder):
                shutil.rmtree(folder)
            else:
                os.remove(folder)
            current_app.logger.info(f"'{new_basename}' Deleted download '{folder}'")

            try:
                removed = remove_from_queue(service, download_id, delete_files=True)
                current_app.logger.info(
                    f"'{new_basename}' Removed {removed} {service} queue item(s)"
                )
            except Exception as e:
                current_app.logger.warning(
                    f"'{new_basename}' Unable to remove the {service} queue item: {e}"
                )
            redis.hdel(PENDING_KEY, download_id)

        except Exception as e:
            current_app.logger.error(f"'{new_basename}' Manual import failed: {e}")
            if os.path.exists(hidden):
                os.remove(hidden)
            raise
