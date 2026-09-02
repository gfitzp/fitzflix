"""Test the per-user watchlist, the funnel stage before the shopping list.

These tests cover the toggles on the film pages and the /watchlist page
with availability. They cover the automatic removal when a watch
arrives from any source. They also cover the Letterboxd watchlist.csv
import and the landing-page integrations."""

import io
import json
import re
import zipfile

from datetime import datetime

from tests.factories import make_movie, make_movie_file

NETFLIX = {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/netflix.jpg"}


def csrf_token_from(page_html):
    """Return the CSRF token from a rendered form."""

    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def admin_id(app):
    """Return the id of the seeded admin user."""

    from app.models import User

    with app.app_context():
        return User.query.filter_by(admin=True).first().id


def entries_for(app, user_id):
    """Return the watchlist movie ids of the user."""

    from app.models import UserWatchlist

    with app.app_context():
        return [
            entry.movie_id
            for entry in UserWatchlist.query.filter_by(user_id=user_id).all()
        ]


def remove_form_fields(page_html, movie_id):
    """Return the remove form of the watchlist page for one movie.

    The fields are what a browser submits: every input that the form
    renders, in DOM order, plus the submit button. Thus, a field
    mismatch between the template and the route shows in the test."""

    # Match on the movie_id INPUT, not on any value attribute. The star
    # ladder of the tile renders buttons whose values collide with small
    # ids. Also require the remove submit, because the Radarr form in the
    # Find menu renders the same hidden fields earlier in the tile.

    form_match = re.search(
        r"<form[^>]*>(?:(?!</form>).)*?"
        rf'name="movie_id"[^>]*value="{movie_id}"(?:(?!</form>).)*?'
        r'name="remove_watchlist_submit"(?:(?!</form>).)*?</form>',
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


def pill_counts(page_html):
    """Return the counts of the filter pills, keyed by bucket.

    Each count renders in a data-watchlist-count span. Thus, a live
    removal can update it."""

    return {
        value: int(count)
        for value, count in re.findall(
            r'data-watchlist-count="([^"]+)">(\d+)<', page_html
        )
    }


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

    # Every film offers the add. For an unowned film, the add is the
    # stage before shopping. For an owned film, the add tracks a specific
    # interest in the library.

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

    # The page now shows the Remove face. Both faces stay in the DOM for
    # the live toggle (#187). The inactive face is hidden with d-none.

    page = admin_client.get(f"/movie/{unowned_id}").get_data(as_text=True)
    assert "data-card-watchlist" in page
    assert "d-none" in re.search(
        r'name="add_watchlist_submit"[^>]*class="([^"]*)"', page
    ).group(1)
    assert "d-none" not in re.search(
        r'name="remove_watchlist_submit"[^>]*class="([^"]*)"', page
    ).group(1)
    # The funnel badge is also live. It is visible now. It was hidden
    # before the add.
    assert "d-none" not in re.search(
        r'class="([^"]*)" data-watchlist-badge', page
    ).group(1)

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
    """Test the personal funnel badges on the movie page.

    While the film is unseen, the watchlist badge and the might-interest
    badge show together. A log flips the row to Seen and removes the
    might-interest badge."""

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
        # The direct diary row bypasses the auto-remove of the log path.
        # Thus, the entry persists. This is the state that a re-add after
        # a watch produces: Seen and the watchlist badge show together.
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

        # The watched film was on the watchlist before the import.

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


def test_watchlist_page_lists_availability_and_removes(app, admin_client, monkeypatch):
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

    # An owned watchlisted film is also listed. It shows the library badge.

    with app.app_context():
        owned = make_movie("Watchlist Owned Tracker", 1995)
        make_movie_file(owned, "Bluray-1080p")
        db.session.add(UserWatchlist(user_id=user_id, movie_id=owned.id))
        db.session.commit()
        owned_id = owned.id

    page = admin_client.get("/watchlist").get_data(as_text=True)
    assert "Watchlist Page Film (1994)" in page
    # The availability and ownership badges moved into the popover
    # (2026-08). The tiles carry the hydrated actions instead.
    assert 'title="Streaming on Netflix"' not in page
    assert 'title="In your Fitzflix library"' not in page
    assert page.count("data-state-movie=") == 2
    # The synopsis is in the poster popover now (#45d).
    assert "A film worth waiting for." not in page
    assert "data-card-url" in page
    assert "Watchlist Owned Tracker (1995)" in page

    # The card of the unowned film serves the availability from the
    # cache that the page warmed, with the mandatory JustWatch credit.
    # The API key only gates the fetch path. The seeded day cache
    # answers.

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    card = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert 'title="Streaming on Netflix"' in card
    assert "Streaming data by JustWatch" in card

    # The poster anchor of each tile opens the movie page (#45d). The
    # stretched-link row overlay went away with the rows. Every poster
    # has its popover.

    assert "stretched-link" not in page
    assert page.count('data-card-url="/movie_card') == 2
    assert f'href="/movie/{owned_id}"' in page

    # The removal posts the form EXACTLY as rendered. A handcrafted POST
    # hid a template bug in the past: hidden_tag() emitted a second,
    # empty movie_id input, and WTForms read that input instead of the
    # real one.

    response = admin_client.post("/watchlist", data=remove_form_fields(page, movie_id))
    assert response.status_code == 302
    assert entries_for(app, user_id) == [owned_id]

    page = admin_client.get("/watchlist").get_data(as_text=True)
    response = admin_client.post("/watchlist", data=remove_form_fields(page, owned_id))
    assert response.status_code == 302
    assert entries_for(app, user_id) == []

    page = admin_client.get("/watchlist").get_data(as_text=True)
    assert "Your watchlist is empty." in page

    # The nav links to the page from every page.

    assert 'href="/watchlist"' in page


def test_review_tmdb_watchlist_add_creates_the_record(app, admin_client, monkeypatch):
    import app.main.discover as discover

    from app.models import Movie

    class FakeTMDB:
        """A canned TMDB response."""

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            """Do nothing. The canned response is never an HTTP error."""

        def json(self):
            """Return the canned payload."""

            return self.payload

    def fake_tmdb_get(url, params=None, **kwargs):
        """Return the movie details for the unowned film."""

        return FakeTMDB(
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
    monkeypatch.setattr(discover, "tmdb_get", fake_tmdb_get)

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

        # The live payload primes the display fields. Thus, the movie
        # page that the redirect opens is not empty while the refresh is
        # queued. The refresh stamp stays unset until the full pass.

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


def test_rail_excludes_watchlisted_films(app, admin_client):
    """Test that a watchlisted film never appears on the streaming rail.

    The rail is only discovery now (2026-08-30). Wanted films appear on
    the watchlist shelf at the top, and only when they are watchable."""

    from app import db
    from app.models import UserWatchlist
    from app.streaming_rail import RAIL_KEY

    user_id = admin_id(app)
    with app.app_context():
        wanted = make_movie("Rail Wanted", 1994, tmdb_id=9307)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))
        db.session.commit()

    def rail_item(tmdb_id, title, score):
        """Return a minimal stored rail entry."""

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

    # The wanted film leaves the rail. With no availability cached, it is
    # not watchable tonight. Thus, the watchlist shelf also skips it.

    assert "Rail Wanted (1994)" not in body
    assert "Rail Unwanted High (1994)" in body


def test_watchlist_shelf_shows_streaming_watchlisted_films(app, admin_client):
    """Test that a streaming watchlisted film appears on the top shelf.

    A watchlisted film that streams on a subscribed service is watchable
    tonight. Thus, it appears on the top watchlist shelf. The answer
    comes from the availability cache, never from a live fetch."""

    from app import db
    from app.models import UserWatchlist
    from app.streaming import AVAILABILITY_KEY
    from app.streaming_rail import RAIL_KEY

    user_id = admin_id(app)
    with app.app_context():
        from app.models import UserStreamingProvider

        db.session.add(
            UserStreamingProvider(
                user_id=user_id, provider_id=8, name="Netflix", logo_path="/n.jpg"
            )
        )
        wanted = make_movie("Rail Wanted", 1994, tmdb_id=9307)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))
        db.session.commit()

    app.redis.set(
        AVAILABILITY_KEY.format(tmdb_id=9307),
        json.dumps(
            {"link": None, "flatrate": [NETFLIX], "ads": [], "rent": [], "buy": []}
        ),
    )
    app.redis.set(
        RAIL_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 02:15",
                "items": [
                    {
                        "tmdb_id": 9307,
                        "title": "Rail Wanted",
                        "year": "1994",
                        "poster_path": None,
                        "runtime": 95,
                        "providers": [{**NETFLIX, "kind": "flatrate"}],
                        "because": ["popular on Netflix"],
                        "score": 1.0,
                    }
                ],
            }
        ),
    )

    body = admin_client.get("/").get_data(as_text=True)
    assert "From your watchlist" in body
    assert "Rail Wanted (1994)" in body
    # When the film is on the watchlist shelf, it never repeats on the
    # rail. The rail was its only other source, and the rail excludes it.
    assert body.count("Rail Wanted (1994)") == 1


def test_watchlist_shelf_takes_the_wanted_films_whole(app, admin_client):
    """Test that the watchlist shelf shows all 12 watchlisted owned films.

    The cap of the watchlist shelf is larger than the cap of a discovery
    shelf. The library rail keeps all 12 of its discovery slots."""

    from app import db
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
    assert "From your watchlist" in body
    shown_wanted = sum(f"Library Wanted {n} (1990)" in body for n in range(12))
    shown_discovery = sum(f"Library Discovery {n} (1991)" in body for n in range(12))
    assert shown_wanted == 12
    assert shown_discovery == 12


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


def test_library_rail_excludes_watchlisted_films(app, admin_client):
    """Test that a watchlisted owned film moves from the rail to the shelf.

    The library rail is only discovery. The top watchlist shelf is
    intent. The film shows exactly 1 time on the page."""

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

    # The owned wanted film is watchable tonight. Thus, it appears on
    # the watchlist shelf exactly 1 time. The rail keeps discovery.

    assert "From your watchlist" in body
    assert body.count("Library Wanted (1995)") == 1
    assert "Library Unwanted High (1994)" in body
    assert body.index("From your watchlist") < body.index("Library Wanted (1995)")
    assert body.index("Library Wanted (1995)") < body.index("From your library")


def test_watchlist_shelf_leads_the_page(app, admin_client):
    """Test that the watchlist shelf renders above every discovery shelf.

    The page first shows "what you already want", then the ways to find
    a different film (requested by Glenn, 2026-08-30)."""

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

    body = admin_client.get("/").get_data(as_text=True)
    assert body.index("From your watchlist") < body.index("From your library")
    for n in range(4):
        assert f"Mix Wanted {n} (1990)" in body
        assert body.index(f"Mix Wanted {n} (1990)") < body.index("From your library")
    for n in range(8):
        assert f"Mix Discovery {n} (1991)" in body
        assert body.index("From your library") < body.index(f"Mix Discovery {n} (1991)")


def test_movie_page_not_interested_toggle(app, admin_client):
    """Test that the \u2715 of the ladder is the only disinterest channel.

    #184 removed the standalone buttons. A zero quick-rating flags an
    unowned, unlogged film and clears its watchlist entry. A second zero
    removes the flag. The buttons render nowhere."""

    import re

    from app import db
    from app.models import UserMovieStatus, UserWatchlist

    user_id = admin_id(app)
    with app.app_context():
        record = make_movie("Refusable Record", 1994, tmdb_id=9320)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=record.id))
        db.session.commit()
        record_id = record.id

    page = admin_client.get(f"/movie/{record_id}").get_data(as_text=True)
    assert 'name="not_interested_submit"' not in page
    assert 'name="interested_submit"' not in page
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    admin_client.post(
        f"/movie/{record_id}",
        data={"csrf_token": token, "quick_rating": "0"},
    )
    with app.app_context():
        assert (
            UserMovieStatus.query.filter_by(
                user_id=user_id, movie_id=record_id, kind="not_interested"
            ).first()
            is not None
        )
        # The mark clears the watchlist entry that contradicts it.
        assert (
            UserWatchlist.query.filter_by(user_id=user_id, movie_id=record_id).first()
            is None
        )

    page = admin_client.get(f"/movie/{record_id}").get_data(as_text=True)
    assert "will not recommend" in page
    assert 'name="interested_submit"' not in page
    assert "Might interest you" not in page

    admin_client.post(
        f"/movie/{record_id}",
        data={"csrf_token": token, "quick_rating": "0"},
    )
    with app.app_context():
        assert (
            UserMovieStatus.query.filter_by(
                user_id=user_id, movie_id=record_id, kind="not_interested"
            ).first()
            is None
        )


def test_movie_page_renders_before_enrichment_arrives(app, admin_client):
    """Test that the movie page renders before the TMDB refresh arrives.

    A new record has its tmdb id but no tmdb_data_as_of yet. A watchlist
    add or a log redirects to this page. The page must render while the
    refresh is still in the queue."""

    from app import db

    with app.app_context():
        movie = make_movie("Watchlist Fresh Record", 2020, tmdb_id=9310)
        db.session.commit()
        movie_id = movie.id

    response = admin_client.get(f"/movie/{movie_id}")
    assert response.status_code == 200
    assert "TMDB data refreshing" in response.get_data(as_text=True)


def test_watchlist_availability_filter(app, admin_client):
    """Test the availability filter pills of the 2026-08 revision.

    The default pill shows all films. The other pills are exclusive
    buckets. Owned wins over streaming, and streaming wins over rental.
    UNAVAILABLE holds the films with a known-empty availability. Each
    pill shows a count. The page reports unfetched films as pending and
    does not file them as unavailable. The removal redirect keeps the
    filter."""

    from app import db
    from app.models import UserStreamingProvider, UserWatchlist

    user_id = admin_id(app)
    with app.app_context():
        db.session.add(
            UserStreamingProvider(
                user_id=user_id, provider_id=8, name="Netflix", logo_path="/n.jpg"
            )
        )
        owned = make_movie("Filter Owned Film", 1990)
        make_movie_file(owned, "Bluray-1080p")
        # Owned AND streaming: owned wins. Thus, it files under local only.
        owned_streaming = make_movie("Filter Owned Streaming Film", 1989, tmdb_id=9400)
        make_movie_file(owned_streaming, "Bluray-1080p")
        streaming = make_movie("Filter Streaming Film", 1991, tmdb_id=9401)
        rentable = make_movie("Filter Rentable Film", 1992, tmdb_id=9402)
        warming = make_movie("Filter Warming Film", 1993, tmdb_id=9403)
        # Streaming AND rentable: the subscription wins over the rental.
        both = make_movie("Filter Both Film", 1994, tmdb_id=9404)
        # Fetched, but no subscribed service of the user carries it.
        nowhere = make_movie("Filter Nowhere Film", 1995, tmdb_id=9405)
        # No TMDB id at all: this is a known negative, never pending.
        untracked = make_movie("Filter Untracked Film", 1996)
        for movie in (
            owned,
            owned_streaming,
            streaming,
            rentable,
            warming,
            both,
            nowhere,
            untracked,
        ):
            db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        owned_id = owned.id

    def cache(tmdb_id, flatrate=(), rent=()):
        app.redis.set(
            f"fitzflix:tmdb:watch-providers:movie:{tmdb_id}",
            json.dumps(
                {
                    "link": None,
                    "flatrate": list(flatrate),
                    "ads": [],
                    "rent": list(rent),
                    "buy": [],
                }
            ),
        )

    cache(9400, flatrate=[NETFLIX])
    cache(9401, flatrate=[NETFLIX])
    cache(9402, rent=[NETFLIX])
    cache(9404, flatrate=[NETFLIX], rent=[NETFLIX])
    cache(9405)
    # 9403 stays uncached: the availability is unknown and warming.

    page = admin_client.get("/watchlist").get_data(as_text=True)
    for title in (
        "Filter Owned Film",
        "Filter Owned Streaming Film",
        "Filter Streaming Film",
        "Filter Rentable Film",
        "Filter Warming Film",
        "Filter Both Film",
        "Filter Nowhere Film",
        "Filter Untracked Film",
    ):
        assert title in page
    # The buckets partition the list: 2 + 2 + 1 + 2 = 7, plus the 1 film
    # that is still warming.
    assert pill_counts(page) == {
        "all": 8,
        "local": 2,
        "services": 2,
        "rent": 1,
        "unavailable": 2,
    }
    assert "continues to get the streaming availability" not in page

    local = admin_client.get("/watchlist?availability=local").get_data(as_text=True)
    assert "Filter Owned Film" in local
    assert "Filter Owned Streaming Film" in local
    assert "Filter Streaming Film" not in local
    assert "continues to get the streaming availability" not in local

    services = admin_client.get("/watchlist?availability=services").get_data(
        as_text=True
    )
    assert "Filter Streaming Film" in services
    assert "Filter Both Film" in services
    assert "Filter Owned Film" not in services
    assert "Filter Owned Streaming Film" not in services
    assert "Filter Rentable Film" not in services
    assert "Filter Warming Film" not in services
    assert "1 film is not on this page yet" in services

    rent = admin_client.get("/watchlist?availability=rent").get_data(as_text=True)
    assert "Filter Rentable Film" in rent
    assert "Filter Both Film" not in rent
    assert "Filter Owned Film" not in rent
    assert "Filter Nowhere Film" not in rent
    assert "continues to get the streaming availability" in rent

    unavailable = admin_client.get("/watchlist?availability=unavailable").get_data(
        as_text=True
    )
    assert "Filter Nowhere Film" in unavailable
    assert "Filter Untracked Film" in unavailable
    assert "Filter Warming Film" not in unavailable
    assert "Filter Rentable Film" not in unavailable
    assert "Filter Owned Film" not in unavailable
    assert "continues to get the streaming availability" in unavailable

    # A removal under a filter redirects back INTO the filter.

    import re

    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', local).group(1)
    response = admin_client.post(
        "/watchlist?availability=local",
        data={
            "csrf_token": token,
            "movie_id": owned_id,
            "remove_watchlist_submit": "Remove from Watchlist",
        },
    )
    assert response.status_code == 302
    assert "availability=local" in response.headers["Location"]


def test_watchlist_title_and_runtime_filters(app, admin_client):
    """Test the title search (#216) and the duration filter (#195).

    Both filters narrow the list before the availability pills count.
    Both use the runtime semantics of the landing page: an unknown
    runtime hides only from a filtered view. Both survive the removal
    redirect. One link clears both."""

    from app import db
    from app.models import UserWatchlist

    user_id = admin_id(app)
    with app.app_context():
        epic = make_movie("Alpha Epic", 1990, tmdb_runtime=150)
        brisk = make_movie("Beta Brisk", 1991, tmdb_runtime=90)
        unknown = make_movie("Gamma Mystery", 1992)
        renamed = make_movie(
            "Delta Disk Name", 1993, tmdb_title="Brisk Renamed", tmdb_runtime=100
        )
        for movie in (epic, brisk, unknown, renamed):
            db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        brisk_id = brisk.id

    page = admin_client.get("/watchlist").get_data(as_text=True)
    for title in ("Alpha Epic", "Beta Brisk", "Gamma Mystery", "Brisk Renamed"):
        assert title in page
    assert pill_counts(page)["all"] == 4
    assert ">Clear</a>" not in page

    # The title search matches the display title AND the TMDB title. The
    # match ignores case. The pills count the narrowed set.

    page = admin_client.get("/watchlist?q=BRISK").get_data(as_text=True)
    assert "Beta Brisk" in page
    assert "Brisk Renamed" in page
    assert "Alpha Epic" not in page
    assert "Gamma Mystery" not in page
    assert pill_counts(page)["all"] == 2
    assert ">Clear</a>" in page

    # The duration filter keeps the films that fit. It hides the unknown
    # runtimes and says so. It captions each tile with its runtime.

    page = admin_client.get("/watchlist?minutes=100").get_data(as_text=True)
    assert "Beta Brisk" in page
    assert "Brisk Renamed" in page
    assert "Alpha Epic" not in page
    assert "Gamma Mystery" not in page
    assert "hides the films that have an unknown runtime" in page
    assert "90 min" in page

    # The filters stack. Fitzflix ignores a nonsense minutes value.

    page = admin_client.get("/watchlist?q=brisk&minutes=95").get_data(as_text=True)
    assert "Beta Brisk" in page
    assert "Brisk Renamed" not in page
    page = admin_client.get("/watchlist?minutes=0").get_data(as_text=True)
    assert pill_counts(page)["all"] == 4

    # A search with no matches offers a link back to the full list.

    page = admin_client.get("/watchlist?q=zzzzzz").get_data(as_text=True)
    assert "No watchlisted films match this search." in page
    assert "Show all 4 watchlisted films" in page

    # A removal under the filters redirects back INTO them.

    page = admin_client.get("/watchlist?q=brisk&minutes=95").get_data(as_text=True)
    response = admin_client.post(
        "/watchlist?q=brisk&minutes=95",
        data=remove_form_fields(page, brisk_id),
    )
    assert response.status_code == 302
    assert "q=brisk" in response.headers["Location"]
    assert "minutes=95" in response.headers["Location"]
    assert entries_for(app, user_id) != []
    assert brisk_id not in entries_for(app, user_id)


def test_watchlist_remove_in_place(app, admin_client):
    """Test that the Remove button of the tile posts in the background.

    The form shows data-card-watchlist plus the remove-cell marker
    (#187). The cell carries its bucket for the pill bookkeeping. A post
    with the card header gets JSON back instead of a redirect."""

    from app import db
    from app.models import UserWatchlist

    user_id = admin_id(app)
    with app.app_context():
        movie = make_movie("In Place Removal", 1990)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get("/watchlist").get_data(as_text=True)
    assert "data-card-watchlist data-watchlist-remove-cell" in page
    assert 'data-bucket="unavailable"' in page

    response = admin_client.post(
        "/watchlist",
        data=remove_form_fields(page, movie_id),
        headers={"X-Requested-With": "card"},
    )
    assert response.status_code == 200
    assert response.get_json() == {"on_watchlist": False}
    assert entries_for(app, user_id) == []
