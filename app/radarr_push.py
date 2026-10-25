"""Send ad-hoc Radarr requests, one film at a time and never automatically.

Glenn revised the original always-sync design. A push of every
watchlist film to Radarr would queue hundreds of downloads and fill
the volume. Thus, a request is a deliberate action for one film. The
action is an entry on the Find menu of unowned movie pages and
watchlist tiles. The matching withdraw exists only as a route. Glenn
removed the Un-request entry in 2026-08. The user removes films in
Radarr itself. Thus, the cached badge set can show such a removal
late, by up to its TTL of 1 hour.
The root folder of Radarr is the library volume itself. Thus, a
granted request downloads, renames, and comes back in through the
existing Radarr webhook. No other connection is necessary.

The shared root folder has one consequence. A TMDB refresh can rename
a movie folder. Radarr then holds a path that no longer exists, sees
no file, and downloads the film again. Thus, the refresh reports each
rename to Radarr with follow_rename. On a change of the TMDB id, the
refresh withdraws the entry of the old id with withdraw_movie.

House settings (specified by Glenn): monitor the movie only, minimum
availability Released, and the "Fitzflix" quality profile. Fitzflix
finds the profile by name, never by a hardcoded id.
"""

import json
import os
import shutil
import time
import traceback

import requests

from flask import current_app

# This is the set of TMDB ids that Radarr manages, cached for 1 hour,
# for the request badges. Fitzflix refreshes the set immediately after
# each push or withdrawal.

RADARR_IDS_KEY = "fitzflix:radarr:tmdb_ids"
RADARR_IDS_TTL = 3600

QUALITY_PROFILE_NAME = "Fitzflix"

# The connect and read timeouts of a Radarr call, in seconds. A host
# that is down fails at the connect. Thus, it costs 5 seconds, not 30.
# The read stays long for the full movie list (#268).

RADARR_TIMEOUT = (5, 30)

# The longest wait for the RenameFiles command of Radarr, in seconds
# (#288).

RADARR_RENAME_WAIT = 120


class RadarrError(Exception):
    """A Radarr request failed in a way that the user must know about.

    The message stays as it is."""


def radarr_configured():
    """Return True if the ad-hoc push can work at all."""

    return bool(
        current_app.config.get("RADARR_URL")
        and current_app.config.get("RADARR_API_KEY")
    )


def _radarr(method, path, payload=None):
    """Make one authenticated JSON call to the Radarr API."""

    r = requests.request(
        method,
        current_app.config["RADARR_URL"] + path,
        json=payload,
        headers={"X-Api-Key": current_app.config["RADARR_API_KEY"]},
        timeout=RADARR_TIMEOUT,
    )
    r.raise_for_status()
    return r.json() if r.content else None


def radarr_tmdb_ids(refresh=False):
    """Return the TMDB ids that Radarr manages now, cached for 1 hour.

    If Fitzflix cannot reach Radarr, this function logs the error and
    returns an empty set. The badges are then incomplete. The buttons
    continue to work."""

    if not refresh:
        cached = current_app.redis.get(RADARR_IDS_KEY)
        if cached is not None:
            return set(json.loads(cached))
    try:
        ids = {movie["tmdbId"] for movie in _radarr("GET", "/api/v3/movie")}
    except Exception as e:
        current_app.logger.warning(f"Radarr: couldn't list movies: {e}")
        return set()
    current_app.redis.set(RADARR_IDS_KEY, json.dumps(sorted(ids)), ex=RADARR_IDS_TTL)
    return ids


def request_movie(tmdb_id):
    """Add one film to Radarr, monitored and searched immediately.

    Radarr requires the full lookup object as the body of the add call.
    It refuses a minimal payload. Thus, the flow is: look up the film,
    add the house settings, then POST.
    """

    profiles = _radarr("GET", "/api/v3/qualityprofile")
    profile = next(
        (
            p
            for p in profiles
            if (p.get("name") or "").lower() == QUALITY_PROFILE_NAME.lower()
        ),
        None,
    )
    if profile is None:
        raise RadarrError(f"Radarr has no '{QUALITY_PROFILE_NAME}' quality profile")
    roots = _radarr("GET", "/api/v3/rootfolder")
    if not roots:
        raise RadarrError("Radarr has no root folder configured")

    movie = _radarr("GET", f"/api/v3/movie/lookup/tmdb?tmdbId={int(tmdb_id)}")
    movie.update(
        {
            "qualityProfileId": profile["id"],
            "rootFolderPath": roots[0]["path"],
            "monitored": True,
            "minimumAvailability": "released",
            "addOptions": {"monitor": "movieOnly", "searchForMovie": True},
        }
    )
    try:
        _radarr("POST", "/api/v3/movie", movie)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            body = e.response.text or ""
            if "already been added" in body:
                raise RadarrError("Radarr already has this film") from e
        raise
    radarr_tmdb_ids(refresh=True)


def withdraw_movie(tmdb_id):
    """Remove one film from Radarr and keep its files on the disk."""

    listing = _radarr("GET", f"/api/v3/movie?tmdbId={int(tmdb_id)}")
    if not listing:
        raise RadarrError("Radarr doesn't have this film")
    _radarr(
        "DELETE",
        f"/api/v3/movie/{listing[0]['id']}"
        "?deleteFiles=false&addImportExclusion=false",
    )
    radarr_tmdb_ids(refresh=True)


def follow_rename(tmdb_id, new_folder):
    """Point the Radarr movie at its renamed folder, then request a rescan.

    Radarr matches the movie by TMDB id, its native key. The new path
    keeps the parent of the old Radarr path. Thus, a Radarr host with a
    different mount point still sees the correct folder. The PUT does
    not move files. Fitzflix moved them already. The RefreshMovie
    command makes Radarr adopt the renamed file at once. This function
    returns True if Radarr manages the film."""

    listing = _radarr("GET", f"/api/v3/movie?tmdbId={int(tmdb_id)}")
    if not listing:
        current_app.logger.info(
            f"Radarr does not manage tmdb {tmdb_id}, no path to follow"
        )
        return False
    return _point_entry_at(listing[0], new_folder)


def follow_import_move(source_folder, new_folder):
    """Point the Radarr movie that owns source_folder at new_folder.

    An import takes the download out of the folder of Radarr and puts
    the localized file at the Fitzflix path. Usually the 2 folders are
    the same. They differ when the naming rules differ, for example a
    slash or an accented letter in the title. Then Radarr sees an empty
    folder and searches again. This function finds the Radarr movie by
    the name of its folder. A TMDB id is not always known at import
    time. This function returns True if Radarr manages the folder."""

    wanted = os.path.basename(source_folder.rstrip("/"))
    for entry in _radarr("GET", "/api/v3/movie"):
        if os.path.basename((entry.get("path") or "").rstrip("/")) == wanted:
            return _point_entry_at(entry, new_folder)
    current_app.logger.info(f"Radarr does not manage folder {wanted!r}, skipping")
    return False


def enqueue_radarr_push(func, *args, description):
    """Queue a Radarr push as its own job on the request queue (#268).

    A TMDB refresh and an import run on the single sql worker. A push
    there made that queue wait while Radarr did not answer. Now the
    push runs after the commit, on its own. A failed push is logged.
    The nightly path check repairs what it left behind."""

    current_app.request_queue.enqueue(
        func,
        args=args,
        job_timeout=300,
        result_ttl=86400,
        description=description,
    )


def follow_rename_task(old_tmdb_id, new_tmdb_id, new_folder):
    """Report a movie rename to Radarr. Log a failure. Do not raise.

    This is a task. If the TMDB id changed, the file belongs to a
    different film now. Thus, the entry of the old id leaves Radarr,
    with its files kept on the disk. Then the entry of the new id, if
    Radarr has one, points at the new folder."""

    import traceback

    from app import get_app

    with get_app().app_context():
        if not radarr_configured():
            return False
        if old_tmdb_id and old_tmdb_id != new_tmdb_id:
            try:
                withdraw_movie(old_tmdb_id)
                current_app.logger.info(
                    f"Radarr entry for tmdb {old_tmdb_id} withdrawn after the "
                    f"record moved to tmdb {new_tmdb_id}"
                )
            except RadarrError:
                pass
            except Exception:
                current_app.logger.warning(
                    f"Radarr withdrawal of tmdb {old_tmdb_id} failed: "
                    + traceback.format_exc()
                )
        if new_tmdb_id and new_folder:
            try:
                follow_rename(new_tmdb_id, new_folder)
            except Exception:
                current_app.logger.warning(
                    f"Radarr path update for tmdb {new_tmdb_id} failed: "
                    + traceback.format_exc()
                )
        return True


def follow_import_move_task(source_folder, new_folder):
    """Point the Radarr movie of an import at its Fitzflix folder.

    This is a task. A failure is logged. It does not raise. The file is
    already in place."""

    import traceback

    from app import get_app

    with get_app().app_context():
        try:
            return follow_import_move(source_folder, new_folder)
        except Exception:
            current_app.logger.warning(
                f"Radarr path update for {source_folder!r} failed: "
                + traceback.format_exc()
            )
            return False


def _point_entry_at(entry, new_folder):
    """Rewrite the path of one Radarr movie, then request a rescan."""

    old_path = (entry.get("path") or "").rstrip("/")
    new_path = os.path.join(os.path.dirname(old_path), new_folder)
    if old_path and old_path != new_path:
        entry["path"] = new_path
        _radarr("PUT", f"/api/v3/movie/{entry['id']}?moveFiles=false", entry)
        current_app.logger.info(f"Radarr path {old_path!r} -> {new_path!r}")
    _radarr(
        "POST", "/api/v3/command", {"name": "RefreshMovie", "movieIds": [entry["id"]]}
    )
    return True


# The nightly path check changes at most this many Radarr entries. More
# mismatches than this point to a systemic cause, for example a changed
# mount or root folder. Then the check changes nothing and warns.

MAX_PATH_FIXES = 20


def reconcile_radarr_paths():
    """Point each Radarr movie at the folder that Fitzflix holds for it (#266).

    This is a task. It runs nightly. A rename or an import reports the
    new folder to Radarr at once. If Radarr is down then, that push is
    lost, and Radarr keeps a path that does not exist. It can then
    download the film again. This check finds such an entry by TMDB id
    and compares its folder with the folder that _movie_folder picks.

    The check repoints an entry only when the Fitzflix folder exists and
    the Radarr folder does not. When both folders exist, it logs the
    case and changes nothing. A film that Fitzflix holds no file for is
    not checked. Radarr can be downloading it."""

    from app import get_app
    from app.models import File, Movie
    from app.tmdb_refresh import _movie_folder

    with get_app().app_context():
        if not radarr_configured():
            return "Radarr is not configured"
        library = current_app.config["MOVIE_LIBRARY"]
        try:
            entries = _radarr("GET", "/api/v3/movie")
        except Exception as e:
            current_app.logger.warning(f"Radarr path check: couldn't list movies: {e}")
            return "Radarr unreachable"

        files_by_tmdb = {}
        for file, tmdb_id in (
            File.query.join(Movie, Movie.id == File.movie_id)
            .filter(Movie.tmdb_id.isnot(None))
            .with_entities(File, Movie.tmdb_id)
        ):
            files_by_tmdb.setdefault(tmdb_id, []).append(file)

        fixes = []
        ambiguous = 0
        for entry in entries:
            files = files_by_tmdb.get(entry.get("tmdbId"))
            if not files:
                continue
            wanted = _movie_folder(files)
            held = os.path.basename((entry.get("path") or "").rstrip("/"))
            if not wanted or wanted == held:
                continue
            wanted_exists = os.path.isdir(os.path.join(library, wanted))
            held_exists = bool(held) and os.path.isdir(os.path.join(library, held))
            if wanted_exists and not held_exists:
                fixes.append((entry, held, wanted))
            else:
                ambiguous += 1
                current_app.logger.warning(
                    f"Radarr path check: tmdb {entry.get('tmdbId')} is at "
                    f"{held!r} in Radarr and at {wanted!r} in Fitzflix. Not "
                    f"changed (Radarr folder exists: {held_exists}, Fitzflix "
                    f"folder exists: {wanted_exists})"
                )

        if len(fixes) > MAX_PATH_FIXES:
            current_app.logger.warning(
                f"Radarr path check: {len(fixes)} entries point at missing "
                f"folders, more than {MAX_PATH_FIXES}. Nothing changed. Check "
                f"the Radarr root folder and the library mount"
            )
            return f"{len(fixes)} mismatches, over the limit"

        fixed = 0
        for entry, held, wanted in fixes:
            try:
                _point_entry_at(entry, wanted)
                fixed += 1
            except Exception as e:
                current_app.logger.warning(
                    f"Radarr path check: couldn't repoint tmdb "
                    f"{entry.get('tmdbId')} from {held!r} to {wanted!r}: {e}"
                )

        current_app.logger.info(
            f"Radarr path check: {len(entries)} Radarr movies, {fixed} repointed, "
            f"{ambiguous} left for review"
        )
        return f"{fixed} repointed, {ambiguous} for review"


def _quality_resource(name):
    """Return the Radarr quality resource of a quality title, revision 1."""

    for definition in _radarr("GET", "/api/v3/qualitydefinition"):
        if definition["quality"]["name"] == name:
            return {
                "quality": definition["quality"],
                "revision": {"version": 1, "real": 0, "isRepack": False},
            }
    raise RadarrError(f"Radarr has no quality named {name!r}")


def _radarr_rename_to_quality(movie_id, movie_file_id, quality_name):
    """Give a Radarr movie file a new quality, and let Radarr rename it.

    Return the new absolute path from the record of Radarr. Radarr names
    the file with its naming format, which holds the quality title. Thus,
    Radarr keeps its record of the file. It does not see a missing file
    (#288)."""

    record = _radarr("GET", f"/api/v3/moviefile/{int(movie_file_id)}")
    record["quality"] = _quality_resource(quality_name)
    _radarr("PUT", f"/api/v3/moviefile/{int(movie_file_id)}", record)
    command = _radarr(
        "POST",
        "/api/v3/command",
        {
            "name": "RenameFiles",
            "movieId": int(movie_id),
            "files": [int(movie_file_id)],
        },
    )
    deadline = time.monotonic() + RADARR_RENAME_WAIT
    while True:
        status = _radarr("GET", f"/api/v3/command/{command['id']}").get("status")
        if status == "completed":
            break
        if status in ("failed", "aborted", "cancelled", "orphaned"):
            raise RadarrError(f"The Radarr rename ended with the status {status!r}")
        if time.monotonic() > deadline:
            raise RadarrError("The Radarr rename did not end in time")
        time.sleep(1)
    return _radarr("GET", f"/api/v3/moviefile/{int(movie_file_id)}")["path"]


def rename_download_locally(downloaded_path, new_path, movie_id):
    """Rename a Radarr download in Fitzflix, then ask Radarr to rescan.

    This was the only way before #288. It stays as the fallback when
    Radarr cannot rename its own file. Then Radarr can lose the file."""

    from app.api.arr import send_arr_command

    if downloaded_path != new_path:
        shutil.move(downloaded_path, new_path)
        current_app.logger.info(f"'{downloaded_path}' renamed as '{new_path}'")
    # Radarr can send a webhook when Fitzflix has no Radarr address. The
    # rename is done, so the caller must still get to the localization.

    if movie_id and radarr_configured():
        current_app.logger.info(
            f"Rescanning movie '{os.path.dirname(downloaded_path)}'"
        )
        send_arr_command(
            "Radarr",
            current_app.config["RADARR_URL"] + "/api/v3/command",
            current_app.config["RADARR_API_KEY"],
            {"name": "RefreshMovie", "movieIds": [int(movie_id)]},
        )


def fallback_rename(downloaded_path, new_path, arr_path, rename_locally):
    """Rename a download in Fitzflix after the arr rename failed, and return
    the path to import.

    The arr can complete its rename after Fitzflix stops the wait (#325).
    For example, the RenameFiles command waits behind a long refresh. Then
    the download is not at its old path. Fitzflix asks the arr for the path
    of its record, then looks for the new name. A failure is logged, and
    the error goes to the job. Thus, the job stays in the failed jobs for a
    retry."""

    try:
        if os.path.exists(downloaded_path):
            rename_locally()
            return new_path
        try:
            path = arr_path()
        except Exception as e:
            current_app.logger.warning(
                f"'{downloaded_path}' is not at its path, and the arr record "
                f"is not available: {e}"
            )
            path = None
        for candidate in (path, new_path):
            if candidate and os.path.exists(candidate):
                current_app.logger.info(
                    f"'{downloaded_path}' The arr renamed it as '{candidate}'"
                )
                return candidate
        raise FileNotFoundError(
            f"'{downloaded_path}' is not at its path, at the arr path "
            f"{path!r}, or at '{new_path}'"
        )
    except Exception:
        current_app.logger.error(
            f"'{downloaded_path}' Fitzflix could not rename or find the "
            f"download. The import is not queued: " + traceback.format_exc()
        )
        raise


def adopt_download_task(movie_id, movie_file_id, downloaded_path, new_path, quality):
    """Give a Radarr download the quality of Fitzflix, then queue its import.

    This is a task (#288). Radarr changes the quality of its file record
    and renames the file. Thus, Radarr keeps the file, and it does not
    download the film again. If Radarr cannot do it, Fitzflix renames the
    file as before. In both cases the import of the file goes on the
    queue after the rename."""

    from app import get_app
    from app.importing import enqueue_localization

    with get_app().app_context():
        try:
            path = _radarr_rename_to_quality(movie_id, movie_file_id, quality)
            if not os.path.exists(path):
                raise RadarrError(f"Radarr reports {path!r}, which does not exist")
            current_app.logger.info(
                f"'{downloaded_path}' Radarr renamed it as '{path}' ({quality})"
            )
        except Exception:
            current_app.logger.warning(
                f"Radarr could not rename '{downloaded_path}'. Fitzflix renames "
                f"it: " + traceback.format_exc()
            )
            path = fallback_rename(
                downloaded_path,
                new_path,
                lambda: _radarr("GET", f"/api/v3/moviefile/{int(movie_file_id)}")[
                    "path"
                ],
                lambda: rename_download_locally(downloaded_path, new_path, movie_id),
            )
        if enqueue_localization(path):
            current_app.logger.info(f"'{path}' Sent to Fitzflix")
        return True
