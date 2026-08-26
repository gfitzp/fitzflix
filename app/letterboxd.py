"""Letterboxd RSS sync: each user's public feed polls into their
diary, hands-free.

The feed is Letterboxd's advertised account surface (their real API is
invite-gated): the latest ~50 diary/review items, each carrying a TMDB
id, the watched date, the rewatch flag, the like, the half-star rating
when one was given, and the review text. Ingest is a merge, never a
blind append — the centerpiece rule is that a feed item COMPLETES the
bare row a Plex scrobble already wrote for the same film on the same
(±1) day, so one viewing stays one row as each system reports in.
"""

import html
import re
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, User, UserMovieReview
from app.richtext import strip_disallowed_tags

app = LocalProxy(get_app)

FEED_URL = "https://letterboxd.com/{username}/rss/"

# Letterboxd rejects clientless user agents

FEED_HEADERS = {"User-Agent": "Mozilla/5.0 (Fitzflix diary sync)"}

NAMESPACES = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
}

# A plain watch's description is boilerplate, not a review

BOILERPLATE_RE = re.compile(r"^\s*(Watched|Rewatched) on \w+ \w+ \d{1,2}, \d{4}\.?\s*$")

# The spoiler checkbox rendered as an injected paragraph — metadata,
# not authored text (the CSV export's review text never contains it)

SPOILER_RE = re.compile(
    r"^\s*(<em>)?\s*This review may contain spoilers\.?\s*(</em>)?\s*$"
)


def parse_letterboxd_feed(xml_text):
    """The feed's diary entries as plain dicts, oldest first.

    Only letterboxd-watch and letterboxd-review items count (lists and
    anything else are skipped), and only when they carry a TMDB id —
    without one there is nothing safe to match. Review text is the
    description's paragraphs minus the poster image and the "Watched
    on …" boilerplate.
    """

    root = ET.fromstring(xml_text)
    entries = []
    for item in root.iter("item"):
        guid = (item.findtext("guid") or "").strip()
        if not guid.startswith(("letterboxd-watch-", "letterboxd-review-")):
            continue
        tmdb_id = item.findtext("tmdb:movieId", namespaces=NAMESPACES)
        if not tmdb_id or not tmdb_id.strip().isdigit():
            continue

        watched = item.findtext("letterboxd:watchedDate", namespaces=NAMESPACES)
        watched_date = None
        if watched:
            try:
                watched_date = datetime.strptime(watched.strip(), "%Y-%m-%d")
            except ValueError:
                pass

        rating = item.findtext("letterboxd:memberRating", namespaces=NAMESPACES)
        rewatch = item.findtext("letterboxd:rewatch", namespaces=NAMESPACES)
        liked = item.findtext("letterboxd:memberLike", namespaces=NAMESPACES)

        # Local wall clock, matching every other date_reviewed writer —
        # naive UTC here pushes an evening log onto the next calendar day

        logged_at = None
        pub_date = item.findtext("pubDate")
        if pub_date:
            try:
                logged_at = (
                    parsedate_to_datetime(pub_date).astimezone().replace(tzinfo=None)
                )
            except (TypeError, ValueError):
                pass

        # The description holds HTML: a poster <p><img/></p>, then the
        # review's paragraphs (or the watch boilerplate)

        text_paragraphs = []
        contains_spoilers = False
        description = item.findtext("description") or ""
        for paragraph in re.findall(
            r"<p>(.*?)</p>", description, flags=re.DOTALL | re.IGNORECASE
        ):
            if "<img" in paragraph.lower():
                continue
            # Letterboxd's inline-markup subset (<i>, <b>, …) survives —
            # it's part of the authored text, and matches what the CSV
            # import stores — while every other tag is dropped. The
            # description ships inside CDATA, so its entities (&quot;,
            # &#039;, …) reach us literally — unescape after the tag
            # pass so an unescaped &lt; can't read as markup

            cleaned = html.unescape(strip_disallowed_tags(paragraph)).strip()
            if not cleaned or BOILERPLATE_RE.match(cleaned):
                continue
            if SPOILER_RE.match(cleaned):
                contains_spoilers = True
                continue
            text_paragraphs.append(cleaned)

        entries.append(
            {
                "guid": guid,
                "tmdb_id": int(tmdb_id.strip()),
                "film_title": (
                    item.findtext("letterboxd:filmTitle", namespaces=NAMESPACES) or ""
                ).strip(),
                "film_year": item.findtext(
                    "letterboxd:filmYear", namespaces=NAMESPACES
                ),
                "watched_date": watched_date,
                "rating": float(rating) if rating else None,
                "rewatch": (rewatch.strip() == "Yes" if rewatch is not None else None),
                "liked": liked is not None and liked.strip() == "Yes",
                "review": "\n\n".join(text_paragraphs),
                "contains_spoilers": contains_spoilers,
                "logged_at": logged_at,
            }
        )

    # Chronological ingest keeps rewatch computation and merges sane

    entries.reverse()
    return entries


def fetch_letterboxd_feed(username):
    """The user's raw feed XML, or None when it can't be fetched."""

    try:
        r = requests.get(
            FEED_URL.format(username=username), headers=FEED_HEADERS, timeout=30
        )
        r.raise_for_status()
        return r.text
    except Exception as e:
        current_app.logger.warning(
            f"Letterboxd feed for '{username}' could not be fetched: {e}"
        )
        return None


def _find_merge_target(user_id, movie_id, watched_date):
    """The guid-less diary row a feed item should claim (the feed sync's merge
    rule, two tiers). First: a row for the same film on the SAME
    calendar day, whatever it holds — the CSV-imported twin of this
    very entry, which must be adopted rather than duplicated (the
    importer matches per film and day for the same reason). Second: a
    BARE row (no rating, no text) within a day either way — the Plex
    scrobble whose clock straddled Letterboxd's calendar date near
    midnight. Date-less items only match date-less rows."""

    base = UserMovieReview.query.filter_by(
        user_id=user_id, movie_id=movie_id, letterboxd_guid=None
    )
    if watched_date is None:
        return base.filter(UserMovieReview.date_watched.is_(None)).first()

    exact = base.filter(
        UserMovieReview.date_watched >= watched_date,
        UserMovieReview.date_watched < watched_date + timedelta(days=1),
    ).first()
    if exact is not None:
        return exact

    return base.filter(
        UserMovieReview.rating.is_(None),
        db.or_(UserMovieReview.review.is_(None), UserMovieReview.review == ""),
        UserMovieReview.date_watched >= watched_date - timedelta(days=1),
        UserMovieReview.date_watched < watched_date + timedelta(days=2),
    ).first()


def _apply_entry_fields(row, entry):
    """Write a feed entry's verdict onto a row. Liked is stored VERBATIM
    (Glenn's rule: a Letterboxd like is its own signal — a sub-3-star
    guilty pleasure keeps its heart), and the feed's rewatch flag is
    authoritative when present."""

    from app.videos import star_rating_fields

    changed = False
    fields = dict(star_rating_fields(entry["rating"]))
    fields["liked"] = entry["liked"]
    fields["contains_spoilers"] = entry["contains_spoilers"]
    fields["review"] = entry["review"] or row.review or ""
    if entry["rewatch"] is not None:
        fields["rewatch"] = entry["rewatch"]
    for field, value in fields.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    # Letterboxd is authoritative for the diary's calendar date. A
    # midnight-stamped row carries no clock knowledge, so when its date
    # disagrees with the feed (the UTC-era day drift, or a date edited
    # on Letterboxd) it follows the feed; a row with a real clock time
    # (a Plex scrobble) keeps its timestamp across the near-midnight
    # straddle instead

    if entry["watched_date"] is not None:
        if row.date_watched is None:
            row.date_watched = entry["watched_date"]
            changed = True
        elif (
            row.date_watched.date() != entry["watched_date"].date()
            and row.date_watched.time() == datetime.min.time()
        ):
            row.date_watched = entry["watched_date"]
            changed = True
    if row.date_reviewed is None and (entry["rating"] is not None or entry["review"]):
        row.date_reviewed = entry["logged_at"] or datetime.now()
        changed = True
    return changed


def sync_letterboxd_feeds():
    """Task: poll every configured user's feed and merge it into their
    diary. Safe to run any time; every path is idempotent."""

    with app.app_context():
        for user in User.query.filter(
            User.letterboxd_username.isnot(None), User.letterboxd_username != ""
        ):
            xml_text = fetch_letterboxd_feed(user.letterboxd_username)
            if not xml_text:
                continue
            try:
                entries = parse_letterboxd_feed(xml_text)
            except ET.ParseError as e:
                current_app.logger.warning(
                    f"Letterboxd feed for '{user.letterboxd_username}' "
                    f"did not parse: {e}"
                )
                continue

            added = updated = completed = 0
            created_movies = []
            for entry in entries:
                result = _ingest_entry(user.id, entry, created_movies)
                if result == "added":
                    added += 1
                elif result == "updated":
                    updated += 1
                elif result == "completed":
                    completed += 1
            db.session.commit()

            # Enrich any movie records the feed created, through the
            # standard two-phase refresh pipeline

            for movie_id, tmdb_id in created_movies:
                current_app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=("Movies", movie_id, tmdb_id),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=f"Refreshing TMDB data for movie {movie_id}",
                )

            if added or updated or completed:
                current_app.logger.info(
                    f"Letterboxd sync for '{user.letterboxd_username}': "
                    f"{added} added, {completed} completed from bare watches, "
                    f"{updated} updated"
                )
                if current_app.redis.set(
                    f"fitzflix:elicit:recompute:{user.id}", "1", nx=True, ex=300
                ):
                    current_app.maintenance_queue.enqueue(
                        "app.recommendations.recompute_recommendations",
                        job_timeout="1h",
                        description="Recomputing film recommendations",
                    )
        return True


def _ingest_entry(user_id, entry, created_movies):
    """Merge one feed entry into the diary: skip an unchanged known
    guid, edit a changed one, complete a matching bare watch, or add a
    fresh row. Returns what happened, for the sync log line."""

    from app.videos import (
        clear_not_interested,
        clear_watchlist,
        find_or_create_tmdb_movie,
    )

    existing = UserMovieReview.query.filter_by(
        user_id=user_id, letterboxd_guid=entry["guid"]
    ).first()
    if existing is not None:
        return "updated" if _apply_entry_fields(existing, entry) else "skipped"

    movie = Movie.query.filter_by(tmdb_id=entry["tmdb_id"]).first()
    if movie is None:
        year = entry["film_year"]
        movie, created = find_or_create_tmdb_movie(
            entry["tmdb_id"],
            entry["film_title"],
            int(year) if year and str(year).strip().isdigit() else None,
        )
        if movie is None:
            return "skipped"
        if created:
            db.session.flush()
            created_movies.append((movie.id, entry["tmdb_id"]))

    target = _find_merge_target(user_id, movie.id, entry["watched_date"])
    if target is not None:
        target.letterboxd_guid = entry["guid"]
        _apply_entry_fields(target, entry)
        clear_watchlist(user_id, movie.id)
        clear_not_interested(user_id, movie.id)
        return "completed"

    rewatch = entry["rewatch"]
    if rewatch is None:
        rewatch = (
            db.session.query(UserMovieReview.id)
            .filter_by(user_id=user_id, movie_id=movie.id)
            .first()
            is not None
        )
    from app.videos import star_rating_fields

    row = UserMovieReview(
        user_id=user_id,
        movie_id=movie.id,
        letterboxd_guid=entry["guid"],
        liked=entry["liked"],
        contains_spoilers=entry["contains_spoilers"],
        review=entry["review"],
        date_watched=entry["watched_date"],
        date_reviewed=(
            (entry["logged_at"] or datetime.now())
            if entry["rating"] is not None or entry["review"]
            else None
        ),
        rewatch=rewatch,
        **star_rating_fields(entry["rating"]),
    )
    db.session.add(row)
    clear_watchlist(user_id, movie.id)
    clear_not_interested(user_id, movie.id)
    return "added"
