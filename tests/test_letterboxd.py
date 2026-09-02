"""Test the Letterboxd RSS sync.

The tests cover the feed parsing and the merge rules that keep one
viewing in one row. The rules are guid idempotence, CSV-twin adoption,
and Plex bare-watch completion."""

from datetime import datetime

from app import db
from app.models import Movie, User, UserMovieReview
from tests.factories import make_movie

FEED_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:letterboxd="https://letterboxd.com" xmlns:tmdb="https://themoviedb.org">
<channel><title>Letterboxd - Test</title>
{items}
</channel></rss>"""

ITEM_TEMPLATE = """<item>
<title>{title}, {year}{stars}</title>
<link>https://letterboxd.com/test/film/x/</link>
<guid isPermaLink="false">{guid}</guid>
<pubDate>{pub_date}</pubDate>
<letterboxd:watchedDate>{watched}</letterboxd:watchedDate>
<letterboxd:rewatch>{rewatch}</letterboxd:rewatch>
<letterboxd:filmTitle>{title}</letterboxd:filmTitle>
<letterboxd:filmYear>{year}</letterboxd:filmYear>
{rating_tag}<letterboxd:memberLike>{liked}</letterboxd:memberLike>
<tmdb:movieId>{tmdb_id}</tmdb:movieId>
<description><![CDATA[ <p><img src="https://a.ltrbxd.com/poster.jpg"/></p> {body} ]]></description>
<dc:creator>Test</dc:creator>
</item>"""


def feed_item(
    guid,
    tmdb_id,
    title="Wavelength",
    year=1967,
    watched="2026-07-09",
    rewatch="No",
    rating=None,
    liked="No",
    body="<p>Watched on Thursday July 9, 2026.</p>",
    pub_date="Fri, 10 Jul 2026 02:55:27 +1200",
):
    rating_tag = (
        f"<letterboxd:memberRating>{rating}</letterboxd:memberRating>\n"
        if rating is not None
        else ""
    )
    return ITEM_TEMPLATE.format(
        guid=guid,
        tmdb_id=tmdb_id,
        title=title,
        year=year,
        watched=watched,
        rewatch=rewatch,
        rating_tag=rating_tag,
        liked=liked,
        body=body,
        stars="",
        pub_date=pub_date,
    )


def build_feed(*items):
    return FEED_TEMPLATE.format(items="\n".join(items))


def admin_id():
    return User.query.first().id


def run_sync(app, xml_text, monkeypatch):
    import app.letterboxd as letterboxd

    monkeypatch.setattr(letterboxd, "fetch_letterboxd_feed", lambda username: xml_text)
    letterboxd.sync_letterboxd_feeds()


def test_parser_reads_fields_and_strips_boilerplate(app):
    from app.letterboxd import parse_letterboxd_feed

    xml = build_feed(
        feed_item(
            "letterboxd-review-2",
            88421,
            rating="3.5",
            liked="Yes",
            body="<p><b>My cat</b> hated the soundtrack.</p>",
            pub_date="Sat, 11 Jul 2026 02:55:27 +1200",
        ),
        feed_item("letterboxd-watch-1", 659994, title="Rams", year=2020),
        """<item><guid isPermaLink="false">letterboxd-list-9</guid></item>""",
    )
    entries = parse_letterboxd_feed(xml)

    # The oldest entry is first. The parser removes the list items
    assert [entry["guid"] for entry in entries] == [
        "letterboxd-watch-1",
        "letterboxd-review-2",
    ]
    watch, review = entries
    assert watch["tmdb_id"] == 659994
    assert watch["rating"] is None
    assert watch["review"] == ""  # the parser removed the boilerplate
    assert watch["watched_date"] == datetime(2026, 7, 9)
    assert review["rating"] == 3.5
    assert review["liked"] is True
    # The inline markup of Letterboxd is part of the written text. It stays
    assert review["review"] == "<b>My cat</b> hated the soundtrack."


def test_parser_strips_spoiler_boilerplate_into_flag(app, monkeypatch):
    from app.letterboxd import parse_letterboxd_feed

    xml = build_feed(
        feed_item(
            "letterboxd-review-843274716",
            718821,
            title="Twisters",
            year=2024,
            rating="2.5",
            body=(
                "<p><em>This review may contain spoilers.</em></p> "
                "<p>It was like a PSA about what not to do during a tornado.</p>"
            ),
        ),
        feed_item("letterboxd-watch-77", 659994, title="Rams", year=2020),
    )
    entries = {entry["guid"]: entry for entry in parse_letterboxd_feed(xml)}
    spoilery = entries["letterboxd-review-843274716"]
    plain = entries["letterboxd-watch-77"]
    assert spoilery["contains_spoilers"] is True
    assert spoilery["review"] == (
        "It was like a PSA about what not to do during a tornado."
    )
    assert plain["contains_spoilers"] is False

    # A row can hold the sentence from before the strip existed. The row
    # repairs itself through the guid-edit path. The flag arrives with it
    with app.app_context():
        user = User.query.first()
        user.letterboxd_username = "test"
        movie = make_movie("Twisters", 2024, tmdb_id=718821)
        db.session.add(
            UserMovieReview(
                user_id=user.id,
                movie_id=movie.id,
                letterboxd_guid="letterboxd-review-843274716",
                review=(
                    "This review may contain spoilers.\n\nIt was like a PSA "
                    "about what not to do during a tornado."
                ),
            )
        )
        db.session.commit()
        run_sync(app, xml, monkeypatch)
        db.session.expire_all()
        row = UserMovieReview.query.filter_by(
            letterboxd_guid="letterboxd-review-843274716"
        ).one()
        assert row.review == (
            "It was like a PSA about what not to do during a tornado."
        )
        assert row.contains_spoilers is True


def test_parser_unescapes_html_entities_in_review_text(app):
    from app.letterboxd import parse_letterboxd_feed

    xml = build_feed(
        feed_item(
            "letterboxd-review-846738055",
            88421,
            rating="4",
            body=(
                "<p>&quot;HERE&#039;S YOUR FETTUCCINE!&quot; I haven&#039;t "
                "laughed that hard at a line in ages &amp; won&#039;t soon "
                "&lt;3</p>"
            ),
        )
    )
    (entry,) = parse_letterboxd_feed(xml)
    assert entry["review"] == (
        "\"HERE'S YOUR FETTUCCINE!\" I haven't laughed that hard at a line "
        "in ages & won't soon <3"
    )


def test_sync_adds_rows_and_is_idempotent(app, monkeypatch):
    with app.app_context():
        user = User.query.first()
        user.letterboxd_username = "test"
        movie = make_movie("Wavelength", 1967, tmdb_id=88421)
        db.session.commit()
        movie_id = movie.id

        xml = build_feed(
            feed_item(
                "letterboxd-review-10",
                88421,
                rating="2.5",
                liked="Yes",
                body="<p>Guilty pleasure.</p>",
            )
        )
        run_sync(app, xml, monkeypatch)

        row = UserMovieReview.query.filter_by(
            letterboxd_guid="letterboxd-review-10"
        ).one()
        assert float(row.rating) == 2.5
        assert row.liked is True  # as given: a sub-3 rating keeps its heart
        assert row.review == "Guilty pleasure."
        assert row.rewatch is False
        assert row.movie_id == movie_id

        # Sync again. No row stacks
        run_sync(app, xml, monkeypatch)
        assert UserMovieReview.query.filter_by(movie_id=movie_id).count() == 1

        # An edited entry with the same guid updates the row in place
        xml = build_feed(
            feed_item(
                "letterboxd-review-10",
                88421,
                rating="4",
                liked="Yes",
                body="<p>On reflection, a masterpiece.</p>",
            )
        )
        run_sync(app, xml, monkeypatch)
        db.session.expire_all()
        row = UserMovieReview.query.filter_by(
            letterboxd_guid="letterboxd-review-10"
        ).one()
        assert float(row.rating) == 4.0
        assert row.review == "On reflection, a masterpiece."


def test_sync_adopts_csv_twin_and_completes_plex_watch(app, monkeypatch):
    from app.videos import star_rating_fields

    with app.app_context():
        user = User.query.first()
        user.letterboxd_username = "test"

        # A row from the CSV import: the same film, the same day, rated
        csv_movie = make_movie("Rams", 2020, tmdb_id=659994)
        db.session.add(
            UserMovieReview(
                user_id=user.id,
                movie_id=csv_movie.id,
                liked=True,
                date_watched=datetime(2026, 7, 18),
                **star_rating_fields(4.0),
            )
        )

        # A Plex scrobble: bare, with a timestamp late on the PREVIOUS day
        plex_movie = make_movie("Heat", 1995, tmdb_id=949)
        db.session.add(
            UserMovieReview(
                user_id=user.id,
                movie_id=plex_movie.id,
                date_watched=datetime(2026, 7, 19, 23, 40),
                **star_rating_fields(None),
            )
        )
        db.session.commit()
        csv_id, plex_id = csv_movie.id, plex_movie.id

        xml = build_feed(
            feed_item(
                "letterboxd-watch-20",
                659994,
                title="Rams",
                year=2020,
                watched="2026-07-18",
                liked="Yes",
            ),
            feed_item(
                "letterboxd-review-21",
                949,
                title="Heat",
                year=1995,
                watched="2026-07-20",
                rating="5",
                liked="Yes",
                body="<p>Pacino and De Niro.</p>",
            ),
        )
        run_sync(app, xml, monkeypatch)

        # The sync adopted the CSV twin. It did not make a duplicate
        assert UserMovieReview.query.filter_by(movie_id=csv_id).count() == 1
        adopted = UserMovieReview.query.filter_by(movie_id=csv_id).one()
        assert adopted.letterboxd_guid == "letterboxd-watch-20"

        # The sync completed the Plex bare watch across the midnight
        # straddle. The watch keeps its clock-accurate timestamp
        assert UserMovieReview.query.filter_by(movie_id=plex_id).count() == 1
        completed = UserMovieReview.query.filter_by(movie_id=plex_id).one()
        assert completed.letterboxd_guid == "letterboxd-review-21"
        assert float(completed.rating) == 5.0
        assert completed.review == "Pacino and De Niro."
        assert completed.date_watched == datetime(2026, 7, 19, 23, 40)


def test_sync_creates_missing_movie_records(app, monkeypatch):
    with app.app_context():
        user = User.query.first()
        user.letterboxd_username = "test"
        db.session.commit()

        xml = build_feed(
            feed_item("letterboxd-watch-30", 424242, title="Obscure Short", year=2019)
        )
        run_sync(app, xml, monkeypatch)

        movie = Movie.query.filter_by(tmdb_id=424242).one()
        assert movie.title == "Obscure Short"
        row = UserMovieReview.query.filter_by(
            letterboxd_guid="letterboxd-watch-30"
        ).one()
        assert row.movie_id == movie.id

        # The new record goes into the standard refresh pipeline
        refreshes = [
            job
            for job in app.request_queue.jobs
            if job.func_name == "app.videos.refresh_tmdb_info"
            and job.args[1] == movie.id
        ]
        assert len(refreshes) == 1


def test_sync_corrects_drifted_dates_on_midnight_rows(app, monkeypatch):
    from app.videos import star_rating_fields

    with app.app_context():
        user = User.query.first()
        user.letterboxd_username = "test"

        # A CSV-era row with a date 1 day late (the UTC drift). Its time
        # is midnight. Thus, it has no clock knowledge, and the date of
        # the feed wins
        drifted_movie = make_movie("Speedy", 1928, tmdb_id=32595)
        db.session.add(
            UserMovieReview(
                user_id=user.id,
                movie_id=drifted_movie.id,
                review="",
                date_watched=datetime(2025, 10, 29),
                **star_rating_fields(None),
            )
        )
        db.session.commit()
        drifted_id = drifted_movie.id

        xml = build_feed(
            feed_item(
                "letterboxd-watch-40",
                32595,
                title="Speedy",
                year=1928,
                watched="2025-10-28",
            )
        )
        run_sync(app, xml, monkeypatch)

        # The sync adopted the row (no duplicate) and set the Letterboxd date
        assert UserMovieReview.query.filter_by(movie_id=drifted_id).count() == 1
        row = UserMovieReview.query.filter_by(movie_id=drifted_id).one()
        assert row.letterboxd_guid == "letterboxd-watch-40"
        assert row.date_watched == datetime(2025, 10, 28)

        # A later date edit on Letterboxd goes into the same row
        xml = build_feed(
            feed_item(
                "letterboxd-watch-40",
                32595,
                title="Speedy",
                year=1928,
                watched="2025-10-27",
            )
        )
        run_sync(app, xml, monkeypatch)
        db.session.expire_all()
        row = UserMovieReview.query.filter_by(movie_id=drifted_id).one()
        assert row.date_watched == datetime(2025, 10, 27)


def test_feed_logged_at_is_local_wall_clock(app):
    import app.letterboxd as letterboxd

    # 02:55 UTC is the previous local evening in each US timezone. The
    # stored value must be the local clock, not naive UTC
    xml = build_feed(
        feed_item(
            "letterboxd-review-50",
            88421,
            rating="4",
            body="<p>Good.</p>",
            pub_date="Fri, 10 Jul 2026 02:55:27 +0000",
        )
    )
    with app.app_context():
        entries = letterboxd.parse_letterboxd_feed(xml)
    from datetime import timezone as tz

    expected = (
        datetime(2026, 7, 10, 2, 55, 27, tzinfo=tz.utc)
        .astimezone()
        .replace(tzinfo=None)
    )
    assert entries[0]["logged_at"] == expected
