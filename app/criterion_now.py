"""The landing page's "On Criterion24/7 now" card (#63).

whatsonnow.criterionchannel.com is the Channel's own public now-playing
page for its 24/7 feed: the current film's title, a More link to the
film's info page, and a server-rendered countdown to the next film
(complete with a literal </snap> typo, so parsing stays lenient). A
poller scrapes it, follows the More link for the "Directed by X • YYYY
• Country" and "Starring …" lines, matches the film to TMDb by title
and year for a directly-linked poster, and stores the lot in Redis.

The poller is self-scheduling: each run re-enqueues itself for just
after the countdown expires, under a deterministic job id so chains
never pile up (an unreadable countdown falls back to trying again in
thirty minutes). A half-hourly cron heartbeat checks the chain's
pulse and polls ONLY when the chain has died — while a poll is booked
for the current film's end, the heartbeat does nothing, so the
showing film is never rescanned on the cron cadence (Glenn's report,
Aug 2026). The card renders for Criterion subscribers and hides
itself once the stored film goes stale.
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

# How long past the expected end the card keeps showing the film — the
# next poll normally lands right at the end, so a long overrun means
# the poller is broken and the card should hide rather than lie

STALE_GRACE = timedelta(minutes=15)

TITLE_RE = re.compile(r'class="whatson__title"[^>]*>\s*(.*?)\s*</h2>', re.S)
MORE_RE = re.compile(
    r'<a href="(https://www\.criterionchannel\.com/[^"]+)"[^>]*'
    r'class="[^"]*whatson__channel-link--more'
)

# The countdown's closing tag is literally </snap> today; capture up to
# any tag and read the units out of the text

COUNTDOWN_RE = re.compile(
    r"Next film starts in:.*?whatson__eyebrow--bold[^>]*>\s*([^<]*)", re.S
)

INFO_META_RE = re.compile(
    r"Directed by\s+(?P<director>[^•<]+?)\s*•\s*(?P<year>\d{4})\s*•\s*"
    r"(?P<country>[^<\r\n]+)"
)
INFO_STARRING_RE = re.compile(r"Starring\s+(?P<starring>[^<\r\n]+)")


def _clean_text(text, collapse=True):
    """Unescape, then flatten non-breaking spaces to plain ones. The
    Channel's pages carry them raw (\xa0), as &nbsp;, and occasionally
    double-escaped (&amp;nbsp;) — that last form unescapes to the
    literal text "&nbsp;", which would otherwise be captured into a
    displayed value like "&nbsp;Hong Kong"."""

    text = html.unescape(text).replace("\xa0", " ").replace("&nbsp;", " ")
    text = re.sub(r" {2,}", " ", text)
    return text.strip() if collapse else text


def parse_whatson_page(page_html):
    """(title, more_url, minutes-until-next) from the now-playing page;
    (None, None, None) when the title can't be found. A countdown that
    doesn't parse comes back None — the poller falls back to a short
    retry rather than trusting a guess."""

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
    """{director, year, country, starring} from a criterionchannel.com
    film page — the same "Directed by X • YYYY • Country" line the
    leaving tooltips carry, plus the Starring line; values None when
    absent."""

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
    """True when a scraped name plausibly names the same person as one
    TMDb credit. Containment must run both ways — TMDb often carries a
    fuller romanization ('Mabel Cheung Yuen-Ting') than the Channel's
    'Mabel Cheung' — and two shared name tokens also count, which
    covers reversed name order and hyphen differences."""

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
    """(tmdb_id, poster_path) for the airing film, or (None, None).

    Title-and-year search, then the match is verified against TMDb's
    credited directors when both sides know one — a wrong search hit
    must degrade to a plain card, never dress the wrong film's poster
    over the right title. Without a director on both sides, the
    Channel's Starring line stands in: at least one scraped name must
    appear among TMDb's top billing. The director stays the sole
    verifier when it is available — the enriched cast stops at
    TOP_BILLING_CUTOFF, so a cast miss alone must never veto a film
    whose director agrees."""

    if not info["year"]:
        return None, None
    tmdb_id = match_tmdb_id(title, info["year"])
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
                f"Criterion24/7: TMDb {tmdb_id} credits "
                f"{', '.join(credited)} but the Channel says "
                f"'{info['director']}' — treating as unmatched"
            )
            return None, None
    elif scraped_stars and cast:
        if not any(
            _person_matches(scraped, name) for scraped in scraped_stars for name in cast
        ):
            current_app.logger.warning(
                f"Criterion24/7: TMDb {tmdb_id} bills {', '.join(cast)} "
                f"but the Channel says '{info['starring']}' — "
                f"treating as unmatched"
            )
            return None, None
    return tmdb_id, payload.get("poster_path")


def poll_criterion_now():
    """Task: scrape the now-playing page, enrich and store the current
    film, and re-schedule for just after it ends."""

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

                # A year is enough to try TMDb; the poster comes as a
                # direct TMDb link on a verified match, and the card
                # renders plain otherwise — never a loud guess

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

        # Always re-schedule — a broken run heals itself on the next
        # attempt. Just past the countdown, clamped sane; the
        # deterministic job id keeps the chain single-file even when
        # the cron heartbeat also fires

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
    """Task: revive the self-scheduling poller if its chain has died.

    The half-hourly cron lands here, never on the poller itself: while
    a poll is booked for the current film's end (or queued, or
    running), the heartbeat does nothing. Only a missing chain gets a
    fresh poll — so the currently-showing film is rescanned when it
    ends, not every thirty minutes.

    Aliveness reads the QUEUE REGISTRIES, never the job hash's status:
    the poll re-enqueues itself under its own executing job id, and
    when that run completes RQ writes "finished" over the hash —
    clobbering the "scheduled" the re-enqueue just wrote. The
    scheduled-registry entry is the truth (the live drill caught the
    hash lying "finished" on a healthy chain, Aug 2026)."""

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


def criterion_now_card(user):
    """The now-playing card for one user, or None: Criterion
    subscribers only, and only while the stored film is fresh."""

    subscribed = {row.provider_id for row in user.streaming_providers}
    if CRITERION_PROVIDER_ID not in subscribed:
        return None
    payload = current_app.redis.get(NOW_KEY)
    if not payload:
        return None
    stored = json.loads(payload)

    next_at = None
    if stored.get("ends_at"):
        ends_at = datetime.strptime(stored["ends_at"], "%Y-%m-%d %H:%M:%S")
        if ends_at < datetime.now() - STALE_GRACE:
            return None
        if ends_at > datetime.now():
            next_at = ends_at.strftime("%-I:%M %p")

    tmdb_id = stored.get("tmdb_id")
    payload = enriched_movie(tmdb_id) if tmdb_id else None
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
        "overview": (payload or {}).get("overview"),
        "ladder": _ladder_state_for(user, tmdb_id, payload),
        **_credited_people(payload),
    }


def _credited_people(payload):
    """{directors, cast} as [{id, name}] from an enriched payload —
    credit ids are TMDb person ids, so the card's names can link to
    filmography pages; empty lists on an unmatched film leave the
    scraped text lines to render plain."""

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
    """The card's star-row state: the user's own rating for the airing
    film when they have one, the engine's estimated rating otherwise —
    scored from the enriched payload when the film has no local record,
    the same recipe the movie page uses when it does."""

    # Routes imports this module at startup, so its helpers load lazily

    from app.main.helpers import _latest_review_row
    from app.models import Movie, UserMovieStatus
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
    }
    if not tmdb_id:
        return state

    profile = stored_profile(current_app.redis, user.id)
    movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
    if movie is not None:
        state["movie_id"] = movie.id
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
        # The shared source's tmdb lane — the enriched payload is
        # already cached, so this adds no fetch, and the overlay keeps
        # the number identical to every other surface's
        score = resolved_tmdb_score(current_app.redis, user.id, tmdb_id, profile)
        if score is not None:
            state["estimated"] = estimated_rating(profile, score)
    return state
