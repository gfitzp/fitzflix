"""The per-user watchlist: the funnel stage before the shopping list.
Toggles on film pages, the /watchlist page with availability, automatic
removal when a watch arrives from any source, the Letterboxd
watchlist.csv import, and the landing-page integrations."""

import io
import json
import re
import zipfile

from datetime import datetime

from tests.factories import make_movie, make_movie_file

NETFLIX = {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/netflix.jpg"}


def csrf_token_from(page_html):
    """The CSRF token baked into a rendered form."""

    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def admin_id(app):
    """The seeded admin user's id."""

    from app.models import User

    with app.app_context():
        return User.query.filter_by(admin=True).first().id


def entries_for(app, user_id):
    """The user's watchlist movie ids."""

    from app.models import UserWatchlist

    with app.app_context():
        return [
            entry.movie_id
            for entry in UserWatchlist.query.filter_by(user_id=user_id).all()
        ]


def remove_form_fields(page_html, movie_id):
    """The watchlist page's remove form for one movie, submitted the way
    a browser would: every input the form renders, in DOM order, plus
    the submit button — so template/route field mismatches surface."""

    form_match = re.search(
        r"<form[^>]*>(?:(?!</form>).)*?"
        rf'value="{movie_id}"(?:(?!</form>).)*?</form>',
        page_html,
        re.DOTALL,
    )
    assert form_match, f"no remove form found for movie {movie_id}"
    from werkzeug.datastructures import MultiDict

    fields = [
        (name, value)
        for name, value in re.findall(
            r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form_match.group(0)
        )
    ]
    fields.append(("remove_watchlist_submit", "Remove from Watchlist"))
    return MultiDict(fields)


def test_movie_page_toggle_adds_and_removes(app, admin_client):
    from app import db

    with app.app_context():
        unowned = make_movie(
            "Watchlist Unowned", 1994, tmdb_id=9301, tmdb_data_as_of=datetime.utcnow()
        )
        owned = make_movie(
            "Watchlist Owned", 1995, tmdb_id=9302, tmdb_data_as_of=datetime.utcnow()
        )
        make_movie_file(owned, "Bluray-1080p")
        db.session.commit()
        unowned_id, owned_id = unowned.id, owned.id

    # Every film offers the add — unowned ones as the pre-shopping
    # stage, owned ones to track specific interest within the library

    page = admin_client.get(f"/movie/{unowned_id}").get_data(as_text=True)
    assert 'name="add_watchlist_submit"' in page
    owned_page = admin_client.get(f"/movie/{owned_id}").get_data(as_text=True)
    assert 'name="add_watchlist_submit"' in owned_page

    token = csrf_token_from(page)
    response = admin_client.post(
        f"/movie/{unowned_id}",
        data={"csrf_token": token, "add_watchlist_submit": "Add to Watchlist"},
    )
    assert response.status_code == 302
    user_id = admin_id(app)
    assert entries_for(app, user_id) == [unowned_id]

    # The page now offers removal instead

    page = admin_client.get(f"/movie/{unowned_id}").get_data(as_text=True)
    assert 'name="remove_watchlist_submit"' in page
    assert 'name="add_watchlist_submit"' not in page

    response = admin_client.post(
        f"/movie/{unowned_id}",
        data={
            "csrf_token": token,
            "remove_watchlist_submit": "Remove from Watchlist",
        },
    )
    assert response.status_code == 302
    assert entries_for(app, user_id) == []


def test_movie_page_funnel_badges(app, admin_client):
    """The movie page carries the personal funnel badges: watchlist
    coexists with might-interest while unseen; logging flips the row to
    Seen and retires might-interest."""

    from app import db
    from app.models import User, UserMovieReview, UserWatchlist
    from app.recommendations import RECS_KEY

    user_id = admin_id(app)
    with app.app_context():
        movie = make_movie(
            "Funnel Page Film", 1994, tmdb_id=9601, tmdb_data_as_of=datetime.utcnow()
        )
        make_movie_file(movie, "Bluray-1080p")
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        movie_id = movie.id

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 01:45",
                "items": [{"movie_id": movie_id, "score": 1.0, "because": []}],
            }
        ),
    )

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "On your watchlist" in page
    assert "Might interest you" in page
    assert 'text-bg-info me-1">Seen' not in page

    with app.app_context():
        admin = User.query.filter_by(admin=True).first()
        db.session.add(
            UserMovieReview(
                user_id=admin.id,
                movie_id=movie_id,
                rating=8,
                modified_rating=8,
                whole_stars=4,
                half_stars=0,
            )
        )
        # The direct diary row bypasses the log path's auto-remove, so
        # the entry persists — the state a post-watch re-add would
        # produce: Seen and the watchlist badge coexist
        db.session.commit()

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Seen &mdash; rated 8" in page
    assert "On your watchlist" in page
    assert "Might interest you" not in page


def test_manual_log_clears_the_watchlist_entry(app, admin_client):
    from app import db
    from app.models import UserWatchlist

    user_id = admin_id(app)
    with app.app_context():
        movie = make_movie(
            "Watchlist Logged", 1994, tmdb_id=9303, tmdb_data_as_of=datetime.utcnow()
        )
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": csrf_token_from(page), "review_submit": "Log Movie"},
    )
    assert response.status_code == 302
    assert entries_for(app, user_id) == []


def test_plex_watch_clears_the_watchlist_entry(app):
    from app import db
    from app.models import User, UserWatchlist
    from app.videos import apply_plex_watch

    with app.app_context():
        user = User.query.filter_by(admin=True).first()
        user.plex_username = "watchlist-plexer"
        movie = make_movie("Watchlist Plexed", 1994, tmdb_id=9304)
        db.session.add(UserWatchlist(user_id=user.id, movie_id=movie.id))
        db.session.commit()
        user_id = user.id

    assert (
        apply_plex_watch(9304, "watchlist-plexer", "2026-08-12T01:00:00+00:00", "test")
        is True
    )
    assert entries_for(app, user_id) == []


def test_letterboxd_watchlist_imports_and_watches_clear(app):
    from app import db
    from app.models import UserMovieReview
    from app.models import UserWatchlist
    from app.videos import apply_letterboxd_import, parse_letterboxd_export

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "watchlist.csv",
            "Date,Name,Year,Letterboxd URI\n"
            "2026-08-01,Wanted Import,1994,https://boxd.it/aaa\n",
        )
        zf.writestr(
            "diary.csv",
            "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
            "2026-08-02,Watched Import,1995,https://boxd.it/bbb,4,,,2026-08-01\n",
        )

    films = parse_letterboxd_export(buf.getvalue())
    by_title = {f["title"]: f for f in films}
    assert by_title["Wanted Import"]["watchlist"] is True
    assert by_title["Wanted Import"]["entries"] == []
    assert by_title["Watched Import"]["watchlist"] is False

    user_id = admin_id(app)
    with app.app_context():
        wanted = make_movie("Wanted Import", 1994)
        watched = make_movie("Watched Import", 1995)

        # The watched film sat on the watchlist before the import

        db.session.add(UserWatchlist(user_id=user_id, movie_id=watched.id))
        db.session.commit()
        wanted_id, watched_id = wanted.id, watched.id

        for film in films:
            film["movie_id"] = (
                wanted_id if film["title"] == "Wanted Import" else watched_id
            )

        assert apply_letterboxd_import(user_id, films) is True

        assert entries_for(app, user_id) == [wanted_id]
        assert (
            UserMovieReview.query.filter_by(
                user_id=user_id, movie_id=watched_id
            ).count()
            == 1
        )


def test_watchlist_page_lists_availability_and_removes(app, admin_client):
    from app import db
    from app.models import UserWatchlist

    user_id = admin_id(app)
    with app.app_context():
        from app.models import UserStreamingProvider

        db.session.add(
            UserStreamingProvider(
                user_id=user_id, provider_id=8, name="Netflix", logo_path="/n.jpg"
            )
        )
        movie = make_movie(
            "Watchlist Page Film",
            1994,
            tmdb_id=9305,
            tmdb_overview="A film worth waiting for.",
        )
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        movie_id = movie.id

    app.redis.set(
        "fitzflix:tmdb:watch-providers:movie:9305",
        json.dumps(
            {"link": None, "flatrate": [NETFLIX], "ads": [], "rent": [], "buy": []}
        ),
    )

    # An owned watchlisted film sits alongside, wearing the library badge

    with app.app_context():
        owned = make_movie("Watchlist Owned Tracker", 1995)
        make_movie_file(owned, "Bluray-1080p")
        db.session.add(UserWatchlist(user_id=user_id, movie_id=owned.id))
        db.session.commit()
        owned_id = owned.id

    page = admin_client.get("/watchlist").get_data(as_text=True)
    assert "Watchlist Page Film (1994)" in page
    assert 'title="Streaming on Netflix"' in page
    assert "A film worth waiting for." in page
    assert "Streaming data by JustWatch" in page
    assert "Watchlist Owned Tracker (1995)" in page
    assert page.count('title="In your Fitzflix library"') == 1

    # Whole rows open the movie page (the title's stretched-link covers
    # the row) while each Remove form stays clickable above the overlay

    assert page.count("stretched-link") == 2
    assert f'href="/movie/{owned_id}"' in page

    # Removal posts the form EXACTLY as rendered — a handcrafted POST
    # once hid a template bug where hidden_tag() emitted a second,
    # empty movie_id input that WTForms read instead of the real one

    response = admin_client.post("/watchlist", data=remove_form_fields(page, movie_id))
    assert response.status_code == 302
    assert entries_for(app, user_id) == [owned_id]

    page = admin_client.get("/watchlist").get_data(as_text=True)
    response = admin_client.post("/watchlist", data=remove_form_fields(page, owned_id))
    assert response.status_code == 302
    assert entries_for(app, user_id) == []

    page = admin_client.get("/watchlist").get_data(as_text=True)
    assert "Nothing on your watchlist yet." in page

    # The nav links to the page from everywhere

    assert 'href="/watchlist"' in page


def test_review_tmdb_watchlist_add_creates_the_record(app, admin_client, monkeypatch):
    import app.main.routes as main_routes

    from app.models import Movie

    class FakeTMDb:
        """Canned TMDb response."""

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            """Never an HTTP error."""

        def json(self):
            """The canned payload."""

            return self.payload

    def fake_tmdb_get(url, params=None, **kwargs):
        """Movie details for the unowned film."""

        return FakeTMDb(
            {
                "title": "Wanted Unknown",
                "release_date": "1999-09-09",
                "overview": "Not in the library.",
                "poster_path": "/wanted.jpg",
                "runtime": 90,
                "genres": [],
                "credits": {"cast": []},
                "release_dates": {"results": []},
            }
        )

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(main_routes, "tmdb_get", fake_tmdb_get)

    page = admin_client.get("/review/tmdb/9306").get_data(as_text=True)
    assert 'name="add_watchlist_submit"' in page

    response = admin_client.post(
        "/review/tmdb/9306",
        data={
            "csrf_token": csrf_token_from(page),
            "add_watchlist_submit": "Add to Watchlist",
        },
        follow_redirects=True,
    )

    user_id = admin_id(app)
    with app.app_context():
        movie = Movie.query.filter_by(tmdb_id=9306).one()
        assert entries_for(app, user_id) == [movie.id]
        refresh_jobs = [
            job
            for job in app.request_queue.jobs
            if job.func_name == "app.videos.refresh_tmdb_info"
            and job.args[1] == movie.id
        ]
        assert len(refresh_jobs) == 1

        # The live payload primes the display fields, so the movie page
        # the redirect lands on isn't bare while the refresh is queued —
        # the refresh stamp itself stays unset until the full pass

        assert movie.tmdb_title == "Wanted Unknown"
        assert movie.tmdb_overview == "Not in the library."
        assert movie.tmdb_poster_path == "/wanted.jpg"
        assert movie.tmdb_runtime == 90
        assert movie.tmdb_data_as_of is None

    body = response.get_data(as_text=True)
    assert "Not in the library." in body
    assert "/wanted.jpg" in body
    assert "90&nbsp;minutes" in body
    assert "TMDB data refreshing" in body


def test_rail_pins_and_badges_watchlisted_films(app, admin_client):
    from app import db
    from app.models import UserWatchlist
    from app.streaming_rail import RAIL_KEY

    user_id = admin_id(app)
    with app.app_context():
        wanted = make_movie("Rail Wanted", 1994, tmdb_id=9307)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))
        db.session.commit()

    def rail_item(tmdb_id, title, score):
        """A minimal stored rail entry."""

        return {
            "tmdb_id": tmdb_id,
            "title": title,
            "year": "1994",
            "poster_path": None,
            "runtime": 95,
            "providers": [{**NETFLIX, "kind": "flatrate"}],
            "because": ["popular on Netflix"],
            "score": score,
        }

    app.redis.set(
        RAIL_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 02:15",
                "items": [
                    rail_item(9308, "Rail Unwanted High", 2.0),
                    rail_item(9307, "Rail Wanted", 1.0),
                ],
            }
        ),
    )

    body = admin_client.get("/").get_data(as_text=True)

    # The watchlisted film holds a badged slot alongside the
    # higher-scoring one (positions vary daily since the shuffle)

    assert "On your watchlist" in body
    assert "Rail Wanted (1994)" in body
    assert "Rail Unwanted High (1994)" in body


def test_rail_pin_cap_keeps_the_rotation_alive(app, admin_client):
    """A big watchlist cycles through a few pinned slots instead of
    freezing the streaming rail — discovery keeps most of the cards."""

    from app import db
    from app.main.routes import WATCHLIST_PIN_LIMIT
    from app.models import UserWatchlist
    from app.streaming_rail import RAIL_KEY

    user_id = admin_id(app)
    with app.app_context():
        wanted_tmdb_ids = []
        for n in range(12):
            movie = make_movie(f"Rail Wanted {n}", 1990, tmdb_id=9400 + n)
            db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
            wanted_tmdb_ids.append(9400 + n)
        db.session.commit()

    def rail_item(tmdb_id, title, score):
        """A minimal stored rail entry."""

        return {
            "tmdb_id": tmdb_id,
            "title": title,
            "year": "1990",
            "poster_path": None,
            "runtime": 95,
            "providers": [{**NETFLIX, "kind": "flatrate"}],
            "because": ["popular on Netflix"],
            "score": 2.0,
        }

    items = [
        rail_item(tmdb_id, f"Rail Wanted {n}", 2.0)
        for n, tmdb_id in enumerate(wanted_tmdb_ids)
    ]
    items += [rail_item(9500 + n, f"Rail Discovery {n}", 1.0) for n in range(12)]
    app.redis.set(
        RAIL_KEY.format(user_id=user_id),
        json.dumps({"computed_at": "2026-08-12 02:15", "items": items}),
    )

    body = admin_client.get("/").get_data(as_text=True)
    shown_wanted = sum(f"Rail Wanted {n} (1990)" in body for n in range(12))
    shown_discovery = sum(f"Rail Discovery {n} (1990)" in body for n in range(12))
    assert shown_wanted == WATCHLIST_PIN_LIMIT
    assert shown_discovery == 12 - WATCHLIST_PIN_LIMIT


def test_library_rail_pin_cap_keeps_the_rotation_alive(app, admin_client):
    """Twelve watchlisted owned films must not freeze the library rail:
    the cap keeps the daily discovery slots in the majority."""

    from app import db
    from app.main.routes import WATCHLIST_PIN_LIMIT
    from app.models import UserWatchlist
    from app.recommendations import RECS_KEY

    user_id = admin_id(app)
    rec_items = []
    with app.app_context():
        for n in range(12):
            movie = make_movie(f"Library Wanted {n}", 1990)
            make_movie_file(movie, "Bluray-1080p")
            db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        for n in range(12):
            movie = make_movie(f"Library Discovery {n}", 1991)
            make_movie_file(movie, "Bluray-1080p")
            rec_items.append(
                {"movie_id": movie.id, "score": 1.0, "because": ["Comedy"]}
            )
        db.session.commit()

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps({"computed_at": "2026-08-12 01:45", "items": rec_items}),
    )

    body = admin_client.get("/").get_data(as_text=True)
    shown_wanted = sum(f"Library Wanted {n} (1990)" in body for n in range(12))
    shown_discovery = sum(f"Library Discovery {n} (1991)" in body for n in range(12))
    assert shown_wanted == WATCHLIST_PIN_LIMIT
    assert shown_discovery == 12 - WATCHLIST_PIN_LIMIT


def test_watchlist_feeds_the_taste_profile(app):
    from app import db
    from app.models import UserWatchlist
    from app.recommendations import WATCHLIST_WEIGHT, user_movie_weights

    user_id = admin_id(app)
    with app.app_context():
        wanted = make_movie("Weights Wanted", 1994)
        wanted_id = wanted.id
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted_id))
        db.session.commit()

        weights = user_movie_weights(user_id)

    assert weights[wanted_id] == WATCHLIST_WEIGHT


def test_library_rail_pins_and_badges_watchlisted_films(app, admin_client):
    """A watchlisted owned film pins ahead of the library rail's daily
    rotation, badged — the library is big, these are the wanted ones."""

    from app import db
    from app.models import UserWatchlist
    from app.recommendations import RECS_KEY

    user_id = admin_id(app)
    with app.app_context():
        strong = make_movie("Library Unwanted High", 1994)
        make_movie_file(strong, "Bluray-1080p")
        wanted = make_movie("Library Wanted", 1995)
        make_movie_file(wanted, "Bluray-1080p")
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))
        db.session.commit()
        strong_id, wanted_id = strong.id, wanted.id

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 01:45",
                "items": [
                    {"movie_id": strong_id, "score": 2.0, "because": ["Comedy"]},
                    {"movie_id": wanted_id, "score": 1.0, "because": ["Comedy"]},
                ],
            }
        ),
    )

    body = admin_client.get("/").get_data(as_text=True)

    # The watchlisted film holds a badged slot regardless of its stored
    # ranking (positions vary daily since the shuffle)

    assert "On your watchlist" in body
    assert "Library Wanted (1995)" in body
    assert "Library Unwanted High (1994)" in body


def test_library_rail_mixes_pins_into_the_row(app, admin_client, monkeypatch):
    """The amber cards land on day-varying positions instead of always
    leading the rail (Glenn: no fixed watchlist block up front)."""

    from datetime import date as real_date

    from app import db
    from app.models import UserWatchlist
    from app.recommendations import RECS_KEY

    user_id = admin_id(app)
    rec_items = []
    with app.app_context():
        for n in range(4):
            movie = make_movie(f"Mix Wanted {n}", 1990)
            make_movie_file(movie, "Bluray-1080p")
            db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        for n in range(8):
            movie = make_movie(f"Mix Discovery {n}", 1991)
            make_movie_file(movie, "Bluray-1080p")
            rec_items.append(
                {"movie_id": movie.id, "score": 1.0, "because": ["Comedy"]}
            )
        db.session.commit()

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps({"computed_at": "2026-08-12 01:45", "items": rec_items}),
    )

    def pin_positions(frozen):
        """The rail-order ranks the four amber cards land on."""

        class FrozenDate(real_date):
            """A date whose today() is pinned for deterministic seeds."""

            @classmethod
            def today(cls):
                return cls(*frozen)

        monkeypatch.setattr("app.main.routes.date", FrozenDate)
        body = admin_client.get("/").get_data(as_text=True)
        shown = sorted(
            (body.index(title), title)
            for title in [f"Mix Wanted {n} (1990)" for n in range(4)]
            + [f"Mix Discovery {n} (1991)" for n in range(8)]
            if title in body
        )
        assert len(shown) == 12
        return {
            rank
            for rank, (_, title) in enumerate(shown)
            if title.startswith("Mix Wanted")
        }

    first = pin_positions((2026, 8, 12))
    second = pin_positions((2026, 8, 13))

    # Pins sit at day-varying positions, not a fixed leading block on
    # both days; the arrangement changes between days

    assert not (first == {0, 1, 2, 3} and second == {0, 1, 2, 3})
    assert first != second


def test_movie_page_not_interested_toggle(app, admin_client):
    """An unowned, unlogged record offers Not Interested (#45b):
    marking flags the film, clears any watchlist entry, and suppresses
    the funnel; undoing restores it. Owned films never see the button —
    the rating ladder's zero stars is their channel."""

    import re

    from app import db
    from app.models import UserMovieStatus, UserWatchlist

    user_id = admin_id(app)
    with app.app_context():
        record = make_movie("Refusable Record", 1994, tmdb_id=9320)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=record.id))
        owned = make_movie("Owned Unrefusable", 1995)
        make_movie_file(owned, "Bluray-1080p")
        db.session.commit()
        record_id, owned_id = record.id, owned.id

    page = admin_client.get(f"/movie/{record_id}").get_data(as_text=True)
    assert 'name="not_interested_submit"' in page
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    admin_client.post(
        f"/movie/{record_id}",
        data={"csrf_token": token, "not_interested_submit": "Not Interested"},
    )
    with app.app_context():
        assert (
            UserMovieStatus.query.filter_by(
                user_id=user_id, movie_id=record_id, kind="not_interested"
            ).first()
            is not None
        )
        # Marking clears the contradicting watchlist entry
        assert (
            UserWatchlist.query.filter_by(user_id=user_id, movie_id=record_id).first()
            is None
        )

    page = admin_client.get(f"/movie/{record_id}").get_data(as_text=True)
    assert "won&#39;t be recommended" in page or "won't be recommended" in page
    assert 'name="interested_submit"' in page
    assert "Might interest you" not in page

    admin_client.post(
        f"/movie/{record_id}",
        data={"csrf_token": token, "interested_submit": "Undo Not Interested"},
    )
    with app.app_context():
        assert (
            UserMovieStatus.query.filter_by(
                user_id=user_id, movie_id=record_id, kind="not_interested"
            ).first()
            is None
        )

    # Owned films use the ladder's zero stars, not this button

    page = admin_client.get(f"/movie/{owned_id}").get_data(as_text=True)
    assert 'name="not_interested_submit"' not in page


def test_movie_page_renders_before_enrichment_arrives(app, admin_client):
    """A just-created record has its tmdb id but no tmdb_data_as_of yet —
    the page a watchlist add or log redirects to must render while the
    refresh is still in the queue."""

    from app import db

    with app.app_context():
        movie = make_movie("Watchlist Fresh Record", 2020, tmdb_id=9310)
        db.session.commit()
        movie_id = movie.id

    response = admin_client.get(f"/movie/{movie_id}")
    assert response.status_code == 200
    assert "TMDB data refreshing" in response.get_data(as_text=True)
