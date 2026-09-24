"""The "On Criterion 24/7 now" card of the landing page.

whatsonnow.criterionchannel.com is the public now-playing page of the
Channel for its 24/7 feed. Since 2026-09-24 it is a Next.js app. The
title of the current film is the page heading. The page also embeds
the schedule of the feed as data. Each entry has a start time and an
end time. The page has a Film Page link. A poller scrapes the page. It
follows the Film Page link for the schema.org block of the film. That
block names the director, the release date, the country, and the top
billing. The poller matches the film to TMDB by title and year for a
direct poster link. Then it stores all of this in Redis.

The poller schedules itself. Each run enqueues itself again for a time
just after the current film ends. It uses a deterministic job id. Thus,
the chains never accumulate. If the end time is unreadable, the poller
tries again in 30 minutes. A cron heartbeat runs every 30 minutes. It
checks that the chain is alive. It polls ONLY if the chain is dead.
While a poll is booked for the end of the current film, the heartbeat
does nothing. Thus, Fitzflix never scans the film that is on again on
the cron cadence (reported by Glenn, 2026-08). The card renders for
Criterion subscribers. The card hides itself when the stored film is
stale.
"""

import html
import json
import re
import traceback

from datetime import datetime, timedelta, timezone

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import get_app
from app.leaving_criterion import CRITERION_PROVIDER_ID, match_tmdb_id
from app.streaming_rail import enriched_movie

app = LocalProxy(get_app)

WHATSON_URL = "https://whatsonnow.criterionchannel.com"
CHANNEL_ORIGIN = "https://www.criterionchannel.com"

# The fallback for the Watch Live link. The page carries the current
# link. The poller stores that one. This constant serves only while no
# poll has stored a link yet.

WATCH_LIVE_URL = "https://www.criterionchannel.com/live/1emmgvqX/criterion-24-7"
NOW_KEY = "fitzflix:criterion:now"
SCHEDULE_KEY = "fitzflix:criterion:schedule"
POLL_JOB_ID = "fitzflix-criterion-now-poll"

# The poll runs this long after the end time of the current film. The
# schedule of the page has times to the second. The feed can drift
# from them by some seconds. A poll that runs before the switch reads
# the old film again.

POLL_CUSHION = timedelta(seconds=15)

# The number of upcoming films that the poller stores. The card turns
# over from this list at the end time of the current film. Thus, the
# next film shows before the poll that enriches it.

UPCOMING_COUNT = 6

# The card turns over only from a schedule that a poll stored this
# recently. An older schedule means that the poller is broken. Then
# the card must hide, as before.

SCHEDULE_TRUST = timedelta(hours=6)
STAMP = "%Y-%m-%d %H:%M:%S"

# The time after the expected end. The card continues to show the film
# during this time. The next poll normally arrives exactly at the end.
# Thus, a long overrun means that the poller is broken. Then the card
# must hide and not show incorrect data.

STALE_GRACE = timedelta(minutes=15)

TITLE_RE = re.compile(r'<h1 class="[^"]*__title[^"]*"[^>]*>\s*(.*?)\s*</h1>', re.S)

# The schedule of the feed sits in the data of the Next.js app. There,
# each quotation mark is escaped as \". The same regex must also read
# plain JSON. Thus, the quotation mark is optional-backslash-quote.

_Q = r'\\?"'

# Each field stops at the next quotation mark that is not escaped. A
# match never crosses into the next entry. The tempered dot below
# refuses to step over the start of the next entry.

_FIELD = r"(?:[^\"\\]|\\.)*?"
_WITHIN_ENTRY = r"(?:(?!startTime).)*?"
SCHEDULE_RE = re.compile(
    _Q
    + r"startTime"
    + _Q
    + r":"
    + _Q
    + r"(?P<start>"
    + _FIELD
    + r")"
    + _Q
    + r","
    + _Q
    + r"endTime"
    + _Q
    + r":"
    + _Q
    + r"(?P<end>"
    + _FIELD
    + r")"
    + _Q
    + r","
    + _Q
    + r"episodeTitle"
    + _Q
    + r":"
    + _Q
    + r"(?P<title>"
    + _FIELD
    + r")"
    + _Q
    + r","
    + _WITHIN_ENTRY
    + _Q
    + r"guid"
    + _Q
    + r":"
    + _Q
    + r"(?P<guid>"
    + _FIELD
    + r")"
    + _Q,
    re.S,
)
FILM_LINK_RE = re.compile(r'href="(/films/[^"]+)"')
LIVE_LINK_RE = re.compile(r'href="(https://www\.criterionchannel\.com/live/[^"]+)"')
JSON_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _clean_text(text, collapse=True):
    """Unescape the text, then replace non-breaking spaces with plain spaces.

    The pages of the Channel carry them raw (\xa0), as &nbsp;, and
    sometimes double-escaped (&amp;nbsp;). The last form unescapes to
    the literal text "&nbsp;". Without this step, that text goes into a
    displayed value such as "&nbsp;Hong Kong"."""

    text = html.unescape(text).replace("\xa0", " ").replace("&nbsp;", " ")
    text = re.sub(r" {2,}", " ", text)
    return text.strip() if collapse else text


def _parse_utc(stamp):
    """Return an aware datetime from a schedule stamp, or None."""

    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _schedule_entries(page_html):
    """Return the schedule entries of the page as (start, end, title, guid).

    The entries are in the order of the page. An entry with a time that
    does not parse is left out."""

    entries = []
    for match in SCHEDULE_RE.finditer(page_html):
        start = _parse_utc(match.group("start"))
        end = _parse_utc(match.group("end"))
        if not (start and end):
            continue
        title = _clean_text(re.sub(r"\\+(.)", r"\1", match.group("title")))
        entries.append((start, end, title, match.group("guid")))
    return entries


def _current_slot(entries, title, now):
    """Return the schedule entry of the film with this title, or None.

    The entry whose window contains now wins if its title agrees with
    the heading. At a film boundary the heading and the clock can
    disagree. Then the next entry with this title wins. A title that is
    in no entry gives None. Fitzflix never guesses an end time."""

    wanted = title.casefold()
    fallback = None
    for entry in entries:
        start, end, entry_title, _ = entry
        if end <= now or entry_title.casefold() != wanted:
            continue
        if start <= now:
            return entry
        if fallback is None or start < fallback[0]:
            fallback = entry
    return fallback


def _local_stamp(moment):
    """Return an aware datetime as the local wall-clock string of Fitzflix."""

    return moment.astimezone().strftime(STAMP)


def parse_whatson_page(page_html, now=None):
    """Return the current film and the upcoming films of the now-playing page.

    The result is a dict with title, more_url, starts_at, ends_at, and
    upcoming. The times are aware datetimes, or None. Upcoming is a
    list of dicts with title, more_url, starts_at, and ends_at as local
    wall-clock strings. The result is None if the title is not found.
    An end time that does not parse comes back as None. Then the
    poller does a short retry. It does not trust a guess."""

    title_match = TITLE_RE.search(page_html)
    if not title_match:
        return None
    title = _clean_text(re.sub(r"<[^>]+>", "", title_match.group(1)))

    now = now or datetime.now(timezone.utc)
    entries = _schedule_entries(page_html)
    slot = _current_slot(entries, title, now)

    starts_at = ends_at = guid = None
    if slot:
        starts_at, ends_at, _, guid = slot

    # The Film Page link of the current film. The link that carries the
    # id of the schedule entry wins. Otherwise the 1st film link on the
    # page is the current film.

    more_url = None
    links = FILM_LINK_RE.findall(page_html)
    for link in links:
        if guid and f"/films/{guid}/" in link:
            more_url = CHANNEL_ORIGIN + link
            break
    if more_url is None and links:
        more_url = CHANNEL_ORIGIN + links[0]

    # The films after the current 1, in order. Without a current entry,
    # the films that start after now. The short film link by id
    # redirects to the full film page.

    horizon = ends_at or now
    upcoming = [
        {
            "title": entry_title,
            "more_url": f"{CHANNEL_ORIGIN}/films/{entry_guid}",
            "starts_at": _local_stamp(start),
            "ends_at": _local_stamp(end),
        }
        for start, end, entry_title, entry_guid in sorted(entries)
        if start >= horizon
    ][:UPCOMING_COUNT]

    return {
        "title": title,
        "more_url": more_url,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "upcoming": upcoming,
    }


def parse_watch_live_url(page_html):
    """Return the Watch Live link of the page, or None."""

    match = LIVE_LINK_RE.search(page_html)
    return match.group(1) if match else None


def _names(people):
    """Return the names of a schema.org person list, joined by commas."""

    if isinstance(people, dict):
        people = [people]
    names = [
        person.get("name", "").strip()
        for person in people or []
        if isinstance(person, dict) and person.get("name")
    ]
    return ", ".join(names) or None


def parse_film_info(page_html):
    """Return {director, year, country, starring} from a film page.

    The page is on criterionchannel.com. It carries a schema.org Movie
    block with the director, the release date, the country, and the
    top billing. A value is None if it is absent."""

    info = {"director": None, "year": None, "country": None, "starring": None}
    blocks = []
    for block in JSON_LD_RE.findall(page_html):
        try:
            data = json.loads(html.unescape(block))
        except ValueError:
            continue
        if data.get("@type") in ("Movie", "VideoObject"):
            blocks.append(data)
    if not blocks:
        return info

    # The Movie block and the VideoObject block share most fields. Each
    # field takes the 1st block that has it. Thus, the director of the
    # VideoObject block fills a Movie block without 1.

    blocks.sort(key=lambda data: data.get("@type") != "Movie")
    movie = {}
    for data in reversed(blocks):
        movie.update({key: value for key, value in data.items() if value})

    info["director"] = _names(movie.get("director"))
    info["starring"] = _names(movie.get("actor"))
    info["country"] = _names(movie.get("countryOfOrigin"))
    year_match = re.match(r"(\d{4})", str(movie.get("datePublished") or ""))
    if year_match:
        info["year"] = int(year_match.group(1))
    return info


def _person_matches(scraped, credited_name):
    """Return True if a scraped name can name the same person as a TMDB credit.

    The containment test must run in both directions. TMDB frequently
    carries a longer romanization ('Mabel Cheung Yuen-Ting') than the
    Channel ('Mabel Cheung'). Two shared name tokens also count. That
    covers a reversed name order and hyphen differences."""

    scraped = scraped.lower()
    name = credited_name.lower()
    if name in scraped or scraped in name:
        return True
    if name.split()[-1] in scraped:
        return True
    scraped_tokens = set(re.findall(r"[a-z]+", scraped))
    name_tokens = set(re.findall(r"[a-z]+", name))
    return len(scraped_tokens & name_tokens) >= 2


def matched_film(title, info):
    """Return (tmdb_id, poster_path) for the film that is on, or (None, None).

    This function searches by title and year. Then it verifies the match
    against the credited directors on TMDB, if both sides know one. A
    wrong search hit must degrade to a plain card. It must never show
    the poster of the wrong film over the correct title. If a director
    is not known on both sides, the Starring line of the Channel is the
    verifier. Then a minimum of 1 scraped name must appear in the top
    billing on TMDB. The director is the only verifier if it is
    available. The enriched cast stops at TOP_BILLING_CUTOFF. Thus, a
    cast miss alone must never veto a film whose director agrees."""

    if not info["year"]:
        return None, None
    tmdb_id = match_tmdb_id(title, info["year"], info["director"])
    if not tmdb_id:
        return None, None
    payload = enriched_movie(tmdb_id)
    if not payload:
        return None, None

    credited = [
        person["name"]
        for person in payload.get("crew") or []
        if person.get("job") == "Director" and person.get("name")
    ]
    cast = [
        person["name"] for person in payload.get("cast") or [] if person.get("name")
    ]
    scraped_stars = [
        name.strip()
        for name in re.split(r",|\band\b", info["starring"] or "")
        if name.strip()
    ]
    if info["director"] and credited:
        if not any(_person_matches(info["director"], name) for name in credited):
            current_app.logger.warning(
                f"Criterion 24/7: TMDB {tmdb_id} credits "
                f"{', '.join(credited)} but the Channel says "
                f"'{info['director']}' — treating as unmatched"
            )
            return None, None
    elif scraped_stars and cast:
        if not any(
            _person_matches(scraped, name) for scraped in scraped_stars for name in cast
        ):
            current_app.logger.warning(
                f"Criterion 24/7: TMDB {tmdb_id} bills {', '.join(cast)} "
                f"but the Channel says '{info['starring']}' — "
                f"treating as unmatched"
            )
            return None, None
    return tmdb_id, payload.get("poster_path")


def _film_info_from(more_url):
    """Return the parsed film info of a film page, or the empty info."""

    info = {"director": None, "year": None, "country": None, "starring": None}
    if more_url:
        try:
            info_page = requests.get(more_url, timeout=15)
            info_page.raise_for_status()
            info = parse_film_info(info_page.text)
        except Exception:
            current_app.logger.warning(traceback.format_exc())
    return info


def _enriched_entry(title, more_url):
    """Return {info fields, tmdb_id, poster_path} for a film of the feed.

    A year is sufficient to try TMDB. The poster comes as a direct TMDB
    link on a verified match. Otherwise the card renders plain. It
    never shows a guess."""

    info = _film_info_from(more_url)
    tmdb_id, poster_path = matched_film(title, info)
    return {"tmdb_id": tmdb_id, "poster_path": poster_path, **info}


def poll_criterion_now():
    """Scrape the now-playing page, store the film and the schedule, and poll again.

    This is a task. It enriches the current film and the next film. It
    schedules itself again for a time just after the current film
    ends."""

    with app.app_context():
        next_delay = timedelta(minutes=30)
        try:
            r = requests.get(WHATSON_URL, timeout=15)
            r.raise_for_status()
            now = datetime.now(timezone.utc)
            parsed = parse_whatson_page(r.text, now)
            watch_url = parse_watch_live_url(r.text) or WATCH_LIVE_URL

            if parsed:
                title = parsed["title"]
                ends_at = parsed["ends_at"]
                fetched_at = datetime.now().strftime(STAMP)
                current_app.redis.set(
                    NOW_KEY,
                    json.dumps(
                        {
                            "title": title,
                            "more_url": parsed["more_url"],
                            "watch_url": watch_url,
                            "starts_at": (
                                _local_stamp(parsed["starts_at"])
                                if parsed["starts_at"]
                                else None
                            ),
                            "ends_at": _local_stamp(ends_at) if ends_at else None,
                            "fetched_at": fetched_at,
                            **_enriched_entry(title, parsed["more_url"]),
                        }
                    ),
                    ex=86400,
                )

                # The upcoming films. The 1st gets the same enrichment as
                # the current film. Thus, the card can turn over to it
                # with a poster and credits before the next poll.

                upcoming = parsed["upcoming"]
                if upcoming:
                    upcoming[0].update(
                        _enriched_entry(upcoming[0]["title"], upcoming[0]["more_url"])
                    )
                current_app.redis.set(
                    SCHEDULE_KEY,
                    json.dumps({"fetched_at": fetched_at, "upcoming": upcoming}),
                    ex=86400,
                )

                end_note = (
                    f"next film at {_local_stamp(ends_at)}"
                    if ends_at
                    else "end time unreadable"
                )
                current_app.logger.info(
                    f"Criterion 24/7 now: '{title}', {end_note}, "
                    f"{len(upcoming)} upcoming"
                )
                if ends_at:
                    next_delay = ends_at - now + POLL_CUSHION
            else:
                current_app.logger.warning(
                    "Criterion 24/7: no title found on the now-playing page"
                )
        except Exception:
            current_app.logger.warning(traceback.format_exc())

        # Always schedule again. A broken run repairs itself on the next
        # attempt. The time is just after the end of the film, clamped
        # to a safe range. The deterministic job id keeps the chain to 1
        # job, even when the cron heartbeat also runs.

        delay = max(timedelta(seconds=30), min(next_delay, timedelta(hours=4)))
        current_app.maintenance_queue.enqueue_in(
            delay,
            "app.criterion_now.poll_criterion_now",
            job_timeout=300,
            job_id=POLL_JOB_ID,
            result_ttl=86400,
            description="Checking what's on Criterion 24/7",
        )
        return True


def heartbeat_criterion_now():
    """Start the self-scheduling poller again if its chain is dead.

    This is a task. The cron that runs every 30 minutes arrives here,
    never at the poller itself. While a poll is booked for the end of
    the current film (or queued, or running), the heartbeat does
    nothing. Only a missing chain gets a new poll. Thus, Fitzflix scans
    the film that is on again when it ends, not every 30 minutes.

    The aliveness check reads the QUEUE REGISTRIES, never the status in
    the job hash. The poll enqueues itself again under its own job id
    while that job runs. When that run completes, RQ writes "finished"
    over the hash. That overwrites the "scheduled" that the enqueue just
    wrote. The entry in the scheduled registry is the truth (the live
    drill found the hash with an incorrect "finished" on a healthy
    chain, 2026-08)."""

    with app.app_context():
        queue = current_app.maintenance_queue
        alive = (
            POLL_JOB_ID in queue.scheduled_job_registry.get_job_ids()
            or POLL_JOB_ID in queue.get_job_ids()
            or POLL_JOB_ID in queue.started_job_registry.get_job_ids()
        )
        if alive:
            return True
        current_app.logger.warning(
            "Criterion 24/7 heartbeat: no poll booked, queued, or running — reviving"
        )
        return poll_criterion_now()


def is_criterion_subscriber(user):
    """Return True if the Criterion Channel is in the streaming services of the user.

    This is the gate for the card and for the live-refresh container of
    the home page. The container must render even while no film is on.
    Then a card can appear when the poller stores one."""

    return CRITERION_PROVIDER_ID in {
        row.provider_id for row in user.streaming_providers
    }


def _stored_schedule():
    """Return the upcoming films of the last poll, or an empty list.

    A schedule older than SCHEDULE_TRUST is not returned. Then the
    poller is broken, and the card must not turn over from old data."""

    payload = current_app.redis.get(SCHEDULE_KEY)
    if not payload:
        return []
    stored = json.loads(payload)
    try:
        fetched_at = datetime.strptime(stored.get("fetched_at") or "", STAMP)
    except ValueError:
        return []
    if fetched_at < datetime.now() - SCHEDULE_TRUST:
        return []
    return stored.get("upcoming") or []


def _turned_over(stored, upcoming, now):
    """Return (film, upcoming) after the end of the stored film.

    The stored film is the film of the last poll. When its end time has
    passed, the upcoming entry whose window contains now is the film.
    The entries after it are the new upcoming list. Before the end
    time, or without a matching entry, the stored film stays."""

    ends_at = stored.get("ends_at")
    if not ends_at or datetime.strptime(ends_at, STAMP) > now:
        return stored, upcoming
    for index, entry in enumerate(upcoming):
        starts = datetime.strptime(entry["starts_at"], STAMP)
        ends = datetime.strptime(entry["ends_at"], STAMP)
        if starts <= now < ends + STALE_GRACE:
            film = {**entry, "watch_url": stored.get("watch_url")}
            return film, upcoming[index + 1 :]
    return stored, upcoming


def _up_next(upcoming):
    """Return the preview of the next film of the feed, or None."""

    if not upcoming:
        return None
    entry = upcoming[0]
    starts_at = datetime.strptime(entry["starts_at"], STAMP)
    payload = enriched_movie(entry["tmdb_id"]) if entry.get("tmdb_id") else None
    return {
        "title": entry["title"],
        "year": entry.get("year"),
        "director": entry.get("director"),
        "tmdb_id": entry.get("tmdb_id"),
        "poster_path": entry.get("poster_path"),
        "more_url": entry.get("more_url"),
        "starts_at": starts_at.strftime("%-I:%M %p"),
        "directors": _credited_people(payload)["directors"],
    }


def criterion_now_card(user):
    """Return the now-playing card for one user, or None.

    Only Criterion subscribers get a card, and only while the stored
    film is fresh. After the end time of the stored film, the card
    turns over to the upcoming film of the stored schedule. Thus, the
    next film shows at its start time, before the poll that stores it."""

    if not is_criterion_subscriber(user):
        return None
    payload = current_app.redis.get(NOW_KEY)
    if not payload:
        return None
    now = datetime.now()
    stored, upcoming = _turned_over(json.loads(payload), _stored_schedule(), now)

    next_at = None
    ends_at = None
    if stored.get("ends_at"):
        ends_at = datetime.strptime(stored["ends_at"], STAMP)
        if ends_at < now - STALE_GRACE:
            return None
        if ends_at > now:
            next_at = ends_at.strftime("%-I:%M %p")

    tmdb_id = stored.get("tmdb_id")
    payload = enriched_movie(tmdb_id) if tmdb_id else None

    # The elapsed time of the film. The start time of the schedule
    # gives it directly. Without 1, Fitzflix derives it as the predicted
    # end minus the TMDB runtime. Thus, it is correct even when the
    # heartbeat started a dead chain again during the film and nobody
    # saw the start. An unknown runtime (or an unmatched film) shows
    # nothing. It never shows a guess. The line says "About" because
    # Criterion adds padding between films. After the predicted end
    # (the card stays through STALE_GRACE) the film is over. Then the
    # line disappears.

    minutes_in = None
    runtime = (payload or {}).get("runtime")
    if ends_at is not None and now <= ends_at:
        if stored.get("starts_at"):
            started = datetime.strptime(stored["starts_at"], STAMP)
        elif runtime:
            started = ends_at - timedelta(minutes=runtime)
        else:
            started = None
        if started is not None and started <= now:
            minutes_in = int((now - started).total_seconds() // 60)

    return {
        "title": stored.get("title"),
        "year": stored.get("year"),
        "director": stored.get("director"),
        "country": stored.get("country"),
        "starring": stored.get("starring"),
        "tmdb_id": tmdb_id,
        "poster_path": stored.get("poster_path"),
        "more_url": stored.get("more_url"),
        "watch_url": stored.get("watch_url") or WATCH_LIVE_URL,
        "next_at": next_at,
        "ends_at_epoch_ms": int(ends_at.timestamp() * 1000) if ends_at else None,
        "minutes_in": minutes_in,
        "up_next": _up_next(upcoming),
        # The live refresh of the home page compares this fingerprint
        # between fetches. A changed film replaces the full card. An
        # unchanged film repaints only the status line and the preview.
        "signature": f"{stored.get('title')}|{tmdb_id}|{stored.get('ends_at')}",
        "overview": (payload or {}).get("overview"),
        "ladder": _ladder_state_for(user, tmdb_id, payload),
        **_credited_people(payload),
    }


def _credited_people(payload):
    """Return {directors, cast} as [{id, name}] from an enriched payload.

    The credit ids are TMDB person ids. Thus, the names on the card can
    link to filmography pages. An unmatched film gets empty lists. Then
    the scraped text lines render plain."""

    if not payload:
        return {"directors": [], "cast": []}
    return {
        "directors": [
            {"id": person["id"], "name": person["name"]}
            for person in payload.get("crew") or []
            if person.get("job") == "Director" and person.get("id")
        ],
        "cast": [
            {"id": person["id"], "name": person["name"]}
            for person in (payload.get("cast") or [])[:3]
            if person.get("id")
        ],
    }


def _ladder_state_for(user, tmdb_id, payload):
    """Return the star-row state of the card.

    The state holds the rating of the user for the film that is on, if
    the user has one. Otherwise it holds the estimated rating from the
    engine. If the film has no local record, the score comes from the
    enriched payload. If the film has a local record, the score uses the
    same recipe as the movie page."""

    # Routes imports this module at startup. Thus, its helpers load lazily.

    from app.main.helpers import _latest_review_row, library_upgradable
    from app.models import Movie, UserMovieStatus, UserWatchlist
    from app.recommendations import (
        estimated_rating,
        resolved_score,
        resolved_tmdb_score,
        stored_profile,
    )

    state = {
        "movie_id": None,
        "rating": None,
        "has_review": False,
        "flagged": False,
        "estimated": None,
        # The watchlist toggle of the card reads its state from here.
        "on_watchlist": False,
        # The In-library badge (#160). None = not owned. Otherwise it is
        # the amber or green upgradable verdict that the movie page shows.
        "upgradable": None,
    }
    if not tmdb_id:
        return state

    profile = stored_profile(current_app.redis, user.id)
    movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
    if movie is not None:
        state["movie_id"] = movie.id
        state["upgradable"] = library_upgradable(movie)
        state["on_watchlist"] = (
            UserWatchlist.query.filter_by(
                user_id=int(user.id), movie_id=movie.id
            ).first()
            is not None
        )
        row = _latest_review_row(user.id, movie.id)
        state["has_review"] = row is not None
        if row is not None and row.rating is not None:
            state["rating"] = float(row.rating)
        state["flagged"] = (
            UserMovieStatus.query.filter_by(
                user_id=int(user.id), movie_id=movie.id, kind="not_interested"
            ).first()
            is not None
        )
        if (row is None or row.rating is None) and not state["flagged"]:
            score = resolved_score(current_app.redis, user.id, movie, profile)
            if score is not None:
                state["estimated"] = estimated_rating(profile, score)
    elif profile and payload:
        # This is the tmdb lane of the shared source. The enriched payload
        # is already in the cache. Thus, this adds no fetch. The overlay
        # keeps the number identical to every other surface.
        score = resolved_tmdb_score(current_app.redis, user.id, tmdb_id, profile)
        if score is not None:
            state["estimated"] = estimated_rating(profile, score)
    return state
