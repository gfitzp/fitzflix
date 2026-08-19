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
never pile up; a half-hourly cron heartbeat revives the chain if a
failure ever breaks it. The card renders for Criterion subscribers and
hides itself once the stored film goes stale.
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


def parse_whatson_page(page_html):
    """(title, more_url, minutes-until-next) from the now-playing page;
    (None, None, None) when the title can't be found. A countdown that
    doesn't parse comes back None — the poller falls back to a short
    retry rather than trusting a guess."""

    title_match = TITLE_RE.search(page_html)
    if not title_match:
        return None, None, None
    title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()

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

    text = html.unescape(page_html.replace("&nbsp;", " "))
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

                # A More slug and a year are enough to try TMDb; the
                # poster comes as a direct TMDb link on a match, and
                # the card renders plain otherwise

                tmdb_id = None
                poster_path = None
                if info["year"]:
                    tmdb_id = match_tmdb_id(title, info["year"])
                    if tmdb_id:
                        payload = enriched_movie(tmdb_id)
                        if payload:
                            poster_path = payload.get("poster_path")

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

    return {
        "title": stored.get("title"),
        "year": stored.get("year"),
        "director": stored.get("director"),
        "country": stored.get("country"),
        "starring": stored.get("starring"),
        "tmdb_id": stored.get("tmdb_id"),
        "poster_path": stored.get("poster_path"),
        "more_url": stored.get("more_url"),
        "watch_url": WATCH_LIVE_URL,
        "next_at": next_at,
    }
