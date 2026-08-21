"""TMDb episode-title validation (#78 step 5).

TMDb's TV numbering usually mirrors the TVDB aired order Sonarr used
to number the library's files, but diverges on some shows — and a
mislabeled episode title is worse than none. Before any surface shows
tv_episode titles, each series earns a per-series verdict here: its
TMDb titles are compared against the titles Plex's agents gave the
very same files (matched by Part-file basename, never by title), and
a series whose titles disagree is flagged numbering-suspect so
surfaces can render it plain.

Plex is the only independent corpus: the import rename strips any
title segment from filenames, so the files themselves carry nothing
to compare. Edition-carrying files are excluded as circular —
Fitzflix writes those titles INTO Plex itself (#68).

Verdicts live in the fitzflix:tv:validation Redis hash, one JSON
entry per series id, rebuilt wholesale by each run.
"""

import json
import os
import re
import traceback

from datetime import datetime, timezone
from difflib import SequenceMatcher

from flask import current_app
from unidecode import unidecode
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File, TVEpisode, TVSeries
from app.plex_titles import _plex_get, _tv_section_key

app = LocalProxy(get_app)

VALIDATION_KEY = "fitzflix:tv:validation"

# A verdict needs evidence: fewer comparisons than this and the series
# is merely "unverified", never suspect

MIN_COMPARED = 5

# Below this agreement rate a compared series is numbering-suspect.
# Genuine agreement lands near 1.0 even through formatting noise;
# shifted numbering lands near 0 — the bar just splits the two modes

SUSPECT_BELOW = 0.5

FUZZY_THRESHOLD = 0.75

PAGE_SIZE = 1000


def _normalize(text):
    """Fold a title for fuzzy comparison, the frame game's recipe:
    unaccent, casefold, drop punctuation, strip a leading article,
    collapse whitespace."""

    text = unidecode(text or "").casefold()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = text.split()
    if words and words[0] in ("the", "a", "an"):
        words = words[1:]
    return " ".join(words)


def titles_agree(ours, theirs):
    """True when two episode titles are close enough to be the same
    episode: normalized equality, containment (multi-part naming), or
    a fuzzy ratio over the threshold."""

    a = _normalize(ours)
    b = _normalize(theirs)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= FUZZY_THRESHOLD


def plex_episode_titles():
    """{basename: title} for every titled episode in Plex's TV section,
    matched later to File rows by basename."""

    titles = {}
    section = _tv_section_key()
    if section is None:
        current_app.logger.warning("TV validation: no Plex TV section found")
        return titles

    start = 0
    while True:
        payload = _plex_get(
            f"/library/sections/{section}/all",
            params={
                "type": 4,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": PAGE_SIZE,
            },
        )
        container = payload.get("MediaContainer", {})
        page = container.get("Metadata", []) or []
        for episode in page:
            title = episode.get("title") or ""
            if not title:
                continue
            for media in episode.get("Media", []) or []:
                for part in media.get("Part", []) or []:
                    name = os.path.basename(part.get("file") or "")
                    if name:
                        titles[name] = title
        start += len(page)
        if not page or start >= container.get("totalSize", 0):
            break
    return titles


def compute_validation(plex_titles):
    """{series_id: verdict} comparing each series' tv_episode titles
    against the Plex titles of its own files. Multi-episode spans agree
    when ANY episode in the span matches — Plex titles the file after
    one of its episodes."""

    results = {}
    files = (
        db.session.query(
            File.basename,
            File.series_id,
            File.season,
            File.episode,
            File.last_episode,
        )
        .filter(File.series_id.isnot(None))
        .filter(File.season.isnot(None), File.episode.isnot(None))
        .filter(db.or_(File.edition.is_(None), File.edition == ""))
        .all()
    )
    names = {
        series_id: name
        for series_id, name in db.session.query(
            TVSeries.id, db.func.coalesce(TVSeries.tmdb_name, TVSeries.title)
        )
    }

    for basename, series_id, season, episode, last_episode in files:
        plex_title = plex_titles.get(basename)
        if not plex_title:
            continue

        span = range(episode, (last_episode or episode) + 1)
        rows = (
            TVEpisode.query.filter_by(series_id=series_id, season=season)
            .filter(TVEpisode.episode.in_(list(span)))
            .all()
        )
        our_titles = [row.title for row in rows if row.title]
        if not our_titles:
            continue

        entry = results.setdefault(
            series_id,
            {
                "name": names.get(series_id, str(series_id)),
                "compared": 0,
                "agreed": 0,
                "examples": [],
            },
        )
        entry["compared"] += 1
        if any(titles_agree(ours, plex_title) for ours in our_titles):
            entry["agreed"] += 1
        elif len(entry["examples"]) < 5:
            entry["examples"].append(
                {
                    "season": season,
                    "episode": episode,
                    "plex": plex_title,
                    "tmdb": our_titles[0],
                }
            )

    checked_at = datetime.now(timezone.utc).isoformat()
    for entry in results.values():
        entry["rate"] = entry["agreed"] / entry["compared"]
        entry["suspect"] = (
            entry["compared"] >= MIN_COMPARED and entry["rate"] < SUSPECT_BELOW
        )
        entry["checked_at"] = checked_at
    return results


def validate_tv_titles():
    """Task: rebuild the per-series title verdicts from Plex's current
    titles. No-ops without Plex configuration — leaving any previous
    verdicts in place rather than wiping them."""

    with app.app_context():
        if not (
            current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
        ):
            return True

        try:
            titles = plex_episode_titles()
            if not titles:
                current_app.logger.warning(
                    "TV validation: Plex returned no titled episodes, keeping "
                    "previous verdicts"
                )
                return True

            results = compute_validation(titles)
            pipe = current_app.redis.pipeline()
            pipe.delete(VALIDATION_KEY)
            if results:
                pipe.hset(
                    VALIDATION_KEY,
                    mapping={
                        str(series_id): json.dumps(entry)
                        for series_id, entry in results.items()
                    },
                )
            pipe.execute()

            suspects = [e["name"] for e in results.values() if e["suspect"]]
            current_app.logger.info(
                f"TV validation: {len(results)} series compared, "
                f"{len(suspects)} suspect ({', '.join(suspects) or 'none'})"
            )
        except Exception:
            current_app.logger.error(traceback.format_exc())
            return False

        return True


def validation_report():
    """Every stored verdict, suspects first then by agreement rate —
    the maintenance page's report."""

    raw = current_app.redis.hgetall(VALIDATION_KEY)
    entries = []
    for series_id, blob in raw.items():
        try:
            entry = json.loads(blob)
        except (TypeError, ValueError):
            continue
        entry["series_id"] = int(series_id)
        entries.append(entry)
    entries.sort(key=lambda e: (not e["suspect"], e["rate"], e["name"]))
    return entries


def series_is_suspect(series_id):
    """True when the series' stored verdict is numbering-suspect —
    the render-time gate for episode-title surfaces (#78 step 6).
    Unverified series are trusted: absence of evidence isn't a verdict."""

    blob = current_app.redis.hget(VALIDATION_KEY, str(series_id))
    if not blob:
        return False
    try:
        return bool(json.loads(blob).get("suspect"))
    except (TypeError, ValueError):
        return False
