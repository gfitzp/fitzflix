"""The "On Criterion24/7 now" card of the landing page.

whatsonnow.criterionchannel.com is the public now-playing page of the
Channel for its 24/7 feed. It shows the title of the current film and
a More link to the info page of the film. It also shows a
server-rendered countdown to the next film. The countdown has a
literal </snap> typo. Thus, the
parser stays lenient. A poller scrapes the page. It follows the More
link for the "Directed by X • YYYY • Country" and "Starring …" lines.
It matches the film to TMDB by title and year for a direct poster link.
Then it stores all of this in Redis.

The poller schedules itself. Each run enqueues itself again for a time
just after the countdown expires. It uses a deterministic job id. Thus,
the chains never accumulate. If the countdown is unreadable, the poller
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

from datetime import datetime, timedelta

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import get_app
from app.leaving_criterion import CRITERION_PROVIDER_ID, match_tmdb_id
from app.streaming_rail import enriched_movie

app = LocalProxy(get_app)

WHATSON_URL = "https://whatsonnow.criterionchannel.com"
WATCH_LIVE_URL = "https://www.criterionchannel.com/events/criterion-24-7"
NOW_KEY = "fitzflix:criterion:now"
POLL_JOB_ID = "fitzflix-criterion-now-poll"

# The time after the expected end. The card continues to show the film
# during this time. The next poll normally arrives exactly at the end.
# Thus, a long overrun means that the poller is broken. Then the card
# must hide and not show incorrect data.

STALE_GRACE = timedelta(minutes=15)

TITLE_RE = re.compile(r'class="whatson__title"[^>]*>\s*(.*?)\s*</h2>', re.S)
MORE_RE = re.compile(
    r'<a href="(https://www\.criterionchannel\.com/[^"]+)"[^>]*'
    r'class="[^"]*whatson__channel-link--more'
)

# The closing tag of the countdown is literally </snap> today. Capture
# up to any tag and read the units out of the text.

COUNTDOWN_RE = re.compile(
    r"Next film starts in:.*?whatson__eyebrow--bold[^>]*>\s*([^<]*)", re.S
)

INFO_META_RE = re.compile(
    r"Directed by\s+(?P<director>[^•<]+?)\s*•\s*(?P<year>\d{4})\s*•\s*"
    r"(?P<country>[^<\r\n]+)"
)
INFO_STARRING_RE = re.compile(r"Starring\s+(?P<starring>[^<\r\n]+)")


def _clean_text(text, collapse=True):
    """Unescape the text, then replace non-breaking spaces with plain spaces.

    The pages of the Channel carry them raw (\xa0), as &nbsp;, and
    sometimes double-escaped (&amp;nbsp;). The last form unescapes to
    the literal text "&nbsp;". Without this step, that text goes into a
    displayed value such as "&nbsp;Hong Kong"."""

    text = html.unescape(text).replace("\xa0", " ").replace("&nbsp;", " ")
    text = re.sub(r" {2,}", " ", text)
    return text.strip() if collapse else text


def parse_whatson_page(page_html):
    """Return (title, more_url, minutes-until-next) from the now-playing page.

    The result is (None, None, None) if the title is not found. A
    countdown that does not parse comes back as None. Then the poller
    does a short retry. It does not trust a guess."""

    title_match = TITLE_RE.search(page_html)
    if not title_match:
        return None, None, None
    title = _clean_text(re.sub(r"<[^>]+>", "", title_match.group(1)))

    more_match = MORE_RE.search(page_html)
    more_url = more_match.group(1) if more_match else None

    minutes = None
    countdown_match = COUNTDOWN_RE.search(page_html)
    if countdown_match:
        text = countdown_match.group(1)
        hours_match = re.search(r"(\d+)\s*hour", text)
        minutes_match = re.search(r"(\d+)\s*min", text)
        if hours_match or minutes_match:
            minutes = int(hours_match.group(1) if hours_match else 0) * 60 + int(
                minutes_match.group(1) if minutes_match else 0
            )
    return title, more_url, minutes


def parse_film_info(page_html):
    """Return {director, year, country, starring} from a film page.

    The page is on criterionchannel.com. This is the same "Directed by X
    • YYYY • Country" line that the leaving tooltips carry, plus the
    Starring line. A value is None if it is absent."""

    text = _clean_text(page_html, collapse=False)
    info = {"director": None, "year": None, "country": None, "starring": None}
    meta = INFO_META_RE.search(text)
    if meta:
        info["director"] = meta.group("director").strip()
        info["year"] = int(meta.group("year"))
        info["country"] = meta.group("country").strip()
    starring = INFO_STARRING_RE.search(text)
    if starring:
        info["starring"] = starring.group("starring").strip()
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
                f"Criterion24/7: TMDB {tmdb_id} credits "
                f"{', '.join(credited)} but the Channel says "
                f"'{info['director']}' — treating as unmatched"
            )
            return None, None
    elif scraped_stars and cast:
        if not any(
            _person_matches(scraped, name) for scraped in scraped_stars for name in cast
        ):
            current_app.logger.warning(
                f"Criterion24/7: TMDB {tmdb_id} bills {', '.join(cast)} "
                f"but the Channel says '{info['starring']}' — "
                f"treating as unmatched"
            )
            return None, None
    return tmdb_id, payload.get("poster_path")


def poll_criterion_now():
    """Scrape the now-playing page, store the current film, and schedule again.

    This is a task. It enriches the current film. It schedules itself
    again for a time just after the film ends."""

    with app.app_context():
        next_poll_minutes = 30
        try:
            r = requests.get(WHATSON_URL, timeout=15)
            r.raise_for_status()
            title, more_url, minutes = parse_whatson_page(r.text)

            if title:
                info = {
                    "director": None,
                    "year": None,
                    "country": None,
                    "starring": None,
                }
                if more_url:
                    try:
                        info_page = requests.get(more_url, timeout=15)
                        info_page.raise_for_status()
                        info = parse_film_info(info_page.text)
                    except Exception:
                        current_app.logger.warning(traceback.format_exc())

                # A year is sufficient to try TMDB. The poster comes as a
                # direct TMDB link on a verified match. Otherwise the card
                # renders plain. It never shows a guess.

                tmdb_id, poster_path = matched_film(title, info)

                ends_at = (
                    (datetime.now() + timedelta(minutes=minutes)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    if minutes is not None
                    else None
                )
                current_app.redis.set(
                    NOW_KEY,
                    json.dumps(
                        {
                            "title": title,
                            "more_url": more_url,
                            "tmdb_id": tmdb_id,
                            "poster_path": poster_path,
                            "ends_at": ends_at,
                            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            **info,
                        }
                    ),
                    ex=86400,
                )
                year_note = f" ({info['year']})" if info["year"] else ""
                countdown_note = (
                    f"next film in {minutes} minutes"
                    if minutes is not None
                    else "countdown unreadable"
                )
                current_app.logger.info(
                    f"Criterion24/7 now: '{title}'{year_note}, {countdown_note}"
                )
                if minutes is not None:
                    next_poll_minutes = minutes
            else:
                current_app.logger.warning(
                    "Criterion24/7: no title found on the now-playing page"
                )
        except Exception:
            current_app.logger.warning(traceback.format_exc())

        # Always schedule again. A broken run repairs itself on the next
        # attempt. The time is just after the countdown, clamped to a safe
        # range. The deterministic job id keeps the chain to 1 job, even
        # when the cron heartbeat also runs.

        delay = timedelta(minutes=max(1, min(next_poll_minutes, 240)), seconds=90)
        current_app.maintenance_queue.enqueue_in(
            delay,
            "app.criterion_now.poll_criterion_now",
            job_timeout=300,
            job_id=POLL_JOB_ID,
            result_ttl=86400,
            description="Checking what's on Criterion24/7",
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
            "Criterion24/7 heartbeat: no poll booked, queued, or running — reviving"
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


def criterion_now_card(user):
    """Return the now-playing card for one user, or None.

    Only Criterion subscribers get a card, and only while the stored
    film is fresh."""

    if not is_criterion_subscriber(user):
        return None
    payload = current_app.redis.get(NOW_KEY)
    if not payload:
        return None
    stored = json.loads(payload)

    next_at = None
    ends_at = None
    if stored.get("ends_at"):
        ends_at = datetime.strptime(stored["ends_at"], "%Y-%m-%d %H:%M:%S")
        if ends_at < datetime.now() - STALE_GRACE:
            return None
        if ends_at > datetime.now():
            next_at = ends_at.strftime("%-I:%M %p")

    tmdb_id = stored.get("tmdb_id")
    payload = enriched_movie(tmdb_id) if tmdb_id else None

    # The elapsed time of the film. Fitzflix derives it WITHOUT STATE as
    # the predicted end minus the TMDB runtime. Thus, it is correct even
    # when the heartbeat started a dead chain again during the film and
    # nobody saw the start. An unknown runtime (or an unmatched film)
    # shows nothing. It never shows a guess. The line says "About"
    # because Criterion adds padding between films. The runtime bounds
    # the value. After the predicted end (the card stays through
    # STALE_GRACE) the film is over. "About 110 minutes in" on a
    # 101-minute film would be the guess that this line refuses to make.

    minutes_in = None
    runtime = (payload or {}).get("runtime")
    if ends_at is not None and runtime:
        elapsed = (
            datetime.now() - (ends_at - timedelta(minutes=runtime))
        ).total_seconds() // 60
        if 0 <= elapsed <= runtime:
            minutes_in = int(elapsed)

    return {
        "title": stored.get("title"),
        "year": stored.get("year"),
        "director": stored.get("director"),
        "country": stored.get("country"),
        "starring": stored.get("starring"),
        "tmdb_id": tmdb_id,
        "poster_path": stored.get("poster_path"),
        "more_url": stored.get("more_url"),
        "watch_url": WATCH_LIVE_URL,
        "next_at": next_at,
        "minutes_in": minutes_in,
        # The live refresh of the home page compares this fingerprint
        # between fetches. A changed film replaces the full card. An
        # unchanged film repaints only the status line.
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
