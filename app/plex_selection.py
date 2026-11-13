"""Make the selected streams of Plex match the default tracks of each file (#652).

Plex picks the audio and subtitle streams that it plays from its own
selection, per account and per part. It does not read the flags of the
file at play time. When no account has chosen a stream, Plex picks the
audio stream with the most channels. A film with a mono main track and
a stereo commentary then plays the commentary. A Kubrick film with the
approved mono track first plays the surround remix. Fitzflix puts the
main audio track first and flags it default on import. Thus, Fitzflix
makes the selection of Plex match the file.

The rule: the selected audio stream is the default audio track of the
file, or its first audio track when no track is flagged. When the file
flags a default subtitle track, that track is the selected subtitle
stream. When the file flags none, the subtitle choice of the account
stays as it is. An account can ask Plex for subtitles on every film,
and a file without a flagged subtitle says nothing against that. One
call sets the streams for one part and one account. The call is made
only when the selection differs. Plex keeps the selection in a row per
account and per part, and it keeps that row through an analyze.

The accounts: the owner, through PLEX_TOKEN, and each Plex Home user
that the home switch of plex.tv can mint a token for. A PIN-protected
user needs the PIN, which Fitzflix does not have. A Plex friend outside
the Home keeps their own selection, because the owner holds no token for
their account.

Where it runs: after the Plex analyze of a rewritten file, and after
the `library.new` webhook of a new import. An import that replaces a
file of an item queues that analyze too (#661). A nightly sweep over both
libraries repeats it for each account. Thus, a switch to a commentary track in a
Plex app does not outlive the night. The first track is the default
for a reason. Infuse selects tracks by its own rules.
"""

import time
import traceback
from datetime import timedelta

from flask import current_app
from rq.registry import ScheduledJobRegistry
from werkzeug.local import LocalProxy

from app import get_app, safe_job_id
from app.plex_api import plex_get, plex_write
from app.plex_player import home_user_tokens
from app.tasks import enqueue_task

app = LocalProxy(get_app)

PAGE_SIZE = 1000

# The item type to page for in each kind of section, as in plex_library.

SECTION_ITEM_TYPE = {"movie": 1, "show": 4}

AUDIO = 2
SUBTITLE = 3

# Plex posts `library.new` when it adds an item. Its analysis of the
# file can still run at that moment. The alignment waits this long.

WEBHOOK_DELAY_MINUTES = 2

# An account that cannot read this many items in a row, with none read
# before, has no access to the library. The sweep skips the rest.

NO_ACCESS_FAILURES = 20


def _configured():
    """Return True when a Plex server and its token are set."""

    return bool(
        current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
    )


def _streams(part, kind):
    """Return the streams of one type of a part, in the order of the file."""

    streams = [
        stream
        for stream in part.get("Stream", []) or []
        if stream.get("streamType") == kind and stream.get("id") is not None
    ]
    return sorted(streams, key=lambda stream: stream.get("index") or 0)


def wanted_streams(part):
    """Return (audio id, subtitle id) that the file asks for.

    The audio id is the default audio track, or the first one. It is
    None for a part with no audio stream. The subtitle id is the default
    subtitle track, or None when the file flags no subtitle track. Then
    the file has no opinion, and the account keeps its own choice."""

    audio = _streams(part, AUDIO)
    subtitles = _streams(part, SUBTITLE)
    wanted_audio = next(
        (stream for stream in audio if stream.get("default")),
        audio[0] if audio else None,
    )
    wanted_subtitle = next(
        (stream for stream in subtitles if stream.get("default")), None
    )
    return (
        wanted_audio["id"] if wanted_audio else None,
        wanted_subtitle["id"] if wanted_subtitle else None,
    )


def selected_streams(part):
    """Return (audio id, subtitle id) that Plex has selected for the account.

    The `selected` flags of a part are the flags of the account whose
    token made the read."""

    audio = next(
        (stream["id"] for stream in _streams(part, AUDIO) if stream.get("selected")),
        None,
    )
    subtitle = next(
        (stream["id"] for stream in _streams(part, SUBTITLE) if stream.get("selected")),
        0,
    )
    return audio, subtitle


def needs_selection(part):
    """Return False for a part whose selection cannot differ from the file.

    One audio stream leaves Plex no choice of audio. Without a flagged
    subtitle track, the file asks for no subtitle choice."""

    return len(_streams(part, AUDIO)) > 1 or wanted_streams(part)[1] is not None


def align_part(part, token=None):
    """Set the selection of one part for one account when it differs.

    Return True when a write was made. This is the one function that
    writes a selection."""

    wanted_audio, wanted_subtitle = wanted_streams(part)
    if wanted_audio is None:
        return False
    selected_audio, selected_subtitle = selected_streams(part)
    params = {"allParts": 1}
    if selected_audio != wanted_audio:
        params["audioStreamID"] = wanted_audio
    if wanted_subtitle is not None and selected_subtitle != wanted_subtitle:
        params["subtitleStreamID"] = wanted_subtitle
    if len(params) == 1:
        return False
    plex_write("PUT", f"/library/parts/{part['id']}", params=params, token=token)
    return True


def _parts(item):
    """Yield every part of an item."""

    for media in item.get("Media", []) or []:
        yield from media.get("Part", []) or []


def _metadata(payload):
    """Return the items of a Plex answer, or an empty list."""

    return payload.get("MediaContainer", {}).get("Metadata", []) or []


def accounts():
    """Return [(name, token)]: the owner first, then each Home user.

    The owner has the token None. The Plex helpers then use PLEX_TOKEN.
    A failure of plex.tv leaves the owner alone in the list."""

    found = [("owner", None)]
    try:
        found.extend(home_user_tokens())
    except Exception:
        current_app.logger.warning(traceback.format_exc())
    return found


def align_item(rating_key, token=None):
    """Align every part of one item for one account. Return the number of writes."""

    writes = 0
    for meta in _metadata(plex_get(f"/library/metadata/{rating_key}", token=token)):
        for part in _parts(meta):
            if align_part(part, token):
                writes += 1
    return writes


def _leaf_keys(rating_key):
    """Return the keys of the items with files under this key.

    A movie or an episode is its own leaf. A show or a season has its
    episodes as leaves. Plex posts `library.new` for any of them."""

    metas = _metadata(plex_get(f"/library/metadata/{rating_key}"))
    if not metas:
        return []
    if metas[0].get("type") in ("show", "season"):
        leaves = plex_get(f"/library/metadata/{rating_key}/allLeaves")
        return [
            meta["ratingKey"] for meta in _metadata(leaves) if meta.get("ratingKey")
        ]
    return [str(rating_key)]


def align_plex_items(rating_keys):
    """Align these items for every account. This is a queue task.

    The analyze of a rewritten file and the `library.new` webhook queue
    it. It is best-effort, like every other Plex task here. It returns
    True in all cases."""

    with app.app_context():
        if not _configured():
            return True
        if isinstance(rating_keys, (str, int)):
            rating_keys = [rating_keys]
        keys = []
        for rating_key in rating_keys or []:
            try:
                keys.extend(_leaf_keys(rating_key))
            except Exception:
                current_app.logger.warning(traceback.format_exc())
        if not keys:
            return True
        names = []
        writes = 0
        for name, token in accounts():
            names.append(name)
            for key in keys:
                try:
                    writes += align_item(key, token)
                except Exception:
                    current_app.logger.warning(traceback.format_exc())
        current_app.logger.info(
            f"Plex stream selection: {len(keys)} item(s) checked for "
            f"{len(names)} account(s), {writes} selection(s) set to the default "
            f"tracks of the file"
        )
        return True


def enqueue_align(rating_key, delay_minutes=WEBHOOK_DELAY_MINUTES):
    """Queue the alignment of one item. Return True when a job was queued.

    A second request while the job waits adds nothing. The fixed job id
    makes the two requests one job."""

    if not _configured():
        return False
    job_id = safe_job_id(f"plex_select:{rating_key}")
    job = current_app.maintenance_queue.fetch_job(job_id)
    if job is not None and job.get_status(refresh=False) in ("queued", "scheduled"):
        return False

    # A job that runs marks its hash as finished at its end. A new job
    # under the same id would lose its hash then (#744).

    if job is not None and job.get_status(refresh=False) == "started":
        queue = current_app.maintenance_queue
        waiting = list(queue.job_ids) + ScheduledJobRegistry(queue=queue).get_job_ids()
        if any(waiting_id.startswith(f"{job_id}_") for waiting_id in waiting):
            return False
        job_id = f"{job_id}_{int(time.time())}"
    enqueue_task(
        "app.plex_selection.align_plex_items",
        [str(rating_key)],
        delay=timedelta(minutes=delay_minutes) if delay_minutes else None,
        job_id=job_id,
        description=f"Matching the Plex track selection of item {rating_key}",
    )
    return True


def _section_keys(section):
    """Return the rating keys of the items with files in a section."""

    keys = []
    start = 0
    while True:
        payload = plex_get(
            f"/library/sections/{section['key']}/all",
            params={
                "type": SECTION_ITEM_TYPE[section["type"]],
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": PAGE_SIZE,
            },
        )
        container = payload.get("MediaContainer", {})
        page = container.get("Metadata", []) or []
        keys.extend(meta["ratingKey"] for meta in page if meta.get("ratingKey"))
        start += len(page)
        if not page or start >= container.get("totalSize", 0):
            return keys


def sync_plex_stream_selections():
    """Align every movie and episode for every account. This is a queue task.

    It is the nightly sweep. It reads each item once per account,
    because the `selected` flags are per account. An item whose parts
    have one audio stream and no subtitle stream is read for the owner
    only. Its selection cannot differ for anyone."""

    with app.app_context():
        if not _configured():
            return True
        try:
            payload = plex_get("/library/sections")
            keys = []
            for section in payload.get("MediaContainer", {}).get("Directory", []) or []:
                if section.get("type") in SECTION_ITEM_TYPE:
                    keys.extend(_section_keys(section))
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return True

        settled = set()
        for name, token in accounts():
            read = writes = failures = 0
            for key in keys:
                if token is not None and key in settled:
                    continue
                try:
                    parts = [
                        part
                        for meta in _metadata(
                            plex_get(f"/library/metadata/{key}", token=token)
                        )
                        for part in _parts(meta)
                    ]
                    read += 1
                    if token is None and not any(needs_selection(p) for p in parts):
                        settled.add(key)
                    for part in parts:
                        if align_part(part, token):
                            writes += 1
                except Exception:
                    failures += 1
                    if failures == 1:
                        current_app.logger.warning(traceback.format_exc())
                    if failures >= NO_ACCESS_FAILURES and read == 0:
                        current_app.logger.warning(
                            f"Plex stream selection: '{name}' cannot read the "
                            f"library, skipping this account"
                        )
                        break
            current_app.logger.info(
                f"Plex stream selection: '{name}': {read} item(s) read, {writes} "
                f"selection(s) set to the default tracks of the file, "
                f"{failures} failure(s)"
            )
        return True
