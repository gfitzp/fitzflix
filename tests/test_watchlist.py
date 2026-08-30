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

    # Match on the movie_id INPUT, not any value attribute — the tile's
    # star ladder renders buttons whose values collide with small ids —
    # and require the remove submit, since the Find menu's Radarr form
    # renders the same hidden fields earlier in the tile

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
    """The filter pills' counts, keyed by bucket — each count renders
    in a data-watchlist-count span so a live removal can settle it."""

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

    # The page now shows the Remove face — both faces stay in the DOM
    # for the live toggle (#187), the off one hidden with d-none

    page = admin_client.get(f"/movie/{unowned_id}").get_data(as_text=True)
    assert "data-card-watchlist" in page
    assert "d-none" in re.search(
        r'name="add_watchlist_submit"[^>]*class="([^"]*)"', page
    ).group(1)
    assert "d-none" not in re.search(
        r'name="remove_watchlist_submit"[^>]*class="([^"]*)"', page
    ).group(1)
    # The funnel badge is live too: visible now, hidden before the add
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

    # An owned watchlisted film sits alongside, wearing the library badge

    with app.app_context():
        owned = make_movie("Watchlist Owned Tracker", 1995)
        make_movie_file(owned, "Bluray-1080p")
        db.session.add(UserWatchlist(user_id=user_id, movie_id=owned.id))
        db.session.commit()
        owned_id = owned.id

    page = admin_client.get("/watchlist").get_data(as_text=True)
    assert "Watchlist Page Film (1994)" in page
    # Availability and ownership badges moved into the popover (Aug
    # 2026); the tiles carry the hydrated actions instead
    assert 'title="Streaming on Netflix"' not in page
    assert 'title="In your Fitzflix library"' not in page
    assert page.count("data-state-movie=") == 2
    # The synopsis lives in the poster popover now (#45d)
    assert "A film worth waiting for." not in page
    assert "data-card-url" in page
    assert "Watchlist Owned Tracker (1995)" in page

    # The unowned film's card serves the availability from the cache
    # the page warmed, with the mandatory JustWatch credit (the key
    # only gates the fetch path — the seeded day cache answers)

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    card = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert 'title="Streaming on Netflix"' in card
    assert "Streaming data by JustWatch" in card

    # Each tile's poster anchor opens the movie page (#45d — the
    # stretched-link row overlay is gone with the rows), and every
    # poster is armed with its popover

    assert "stretched-link" not in page
    assert page.count('data-card-url="/movie_card') == 2
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
    import app.main.discover as discover

    from app.models import Movie

    class FakeTMDB:
        """Canned TMDB response."""

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            """Never an HTTP error."""

        def json(self):
            """The canned payload."""

            return self.payload

    def fake_tmdb_get(url, params=None, **kwargs):
        """Movie details for the unowned film."""

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


def test_rail_excludes_watchlisted_films(app, admin_client):
    """A watchlisted film never rides the streaming rail (Aug 30 2026):
    the rail is pure discovery now — the watchlist shelf up top is
    where wanted films surface, and only when they're watchable."""

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

    # The wanted film leaves the rail; with no availability cached it
    # isn't watchable tonight, so the watchlist shelf skips it too

    assert "Rail Wanted (1994)" not in body
    assert "Rail Unwanted High (1994)" in body


def test_watchlist_shelf_shows_streaming_watchlisted_films(app, admin_client):
    """A watchlisted film streaming on a subscribed service is
    watchable tonight, so it surfaces on the top watchlist shelf —
    answered from the availability cache, never a live fetch."""

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
    # Once on the watchlist shelf, the film never repeats on the rail:
    # the rail was its only other source and it's excluded there
    assert body.count("Rail Wanted (1994)") == 1


def test_watchlist_shelf_takes_the_wanted_films_whole(app, admin_client):
    """Twelve watchlisted owned films all surface on the top watchlist
    shelf (its cap is bigger than a discovery shelf's), and the
    library rail keeps all twelve of its discovery slots."""

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
    """A watchlisted owned film leaves the library rail for the top
    watchlist shelf: the rail is pure discovery, the shelf is intent —
    and the film shows exactly once on the page."""

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

    # The owned wanted film is watchable tonight, so it surfaces on
    # the watchlist shelf — once — while the rail keeps discovery

    assert "From your watchlist" in body
    assert body.count("Library Wanted (1995)") == 1
    assert "Library Unwanted High (1994)" in body
    assert body.index("From your watchlist") < body.index("Library Wanted (1995)")
    assert body.index("Library Wanted (1995)") < body.index("From your library")


def test_watchlist_shelf_leads_the_page(app, admin_client):
    """The watchlist shelf renders above every discovery shelf: the
    page reads "what you already want", then ways to find something
    else (Glenn, Aug 30 2026)."""

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
    """The ladder's \u2715 is the one disinterest channel (#184 removed
    the standalone buttons): a zero quick-rating flags an unowned,
    unlogged film and clears its watchlist entry, a second zero unflags
    it, and the buttons render nowhere."""

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
        # Marking clears the contradicting watchlist entry
        assert (
            UserWatchlist.query.filter_by(user_id=user_id, movie_id=record_id).first()
            is None
        )

    page = admin_client.get(f"/movie/{record_id}").get_data(as_text=True)
    assert "won&#39;t be recommended" in page or "won't be recommended" in page
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


def test_watchlist_availability_filter(app, admin_client):
    """The Aug 2026 revision: default shows everything; the other
    pills are exclusive buckets — owned beats streaming beats renting,
    and UNAVAILABLE catches films with a known-empty availability —
    with counts on the pills, unfetched films reported as pending
    instead of filed as unavailable, and the removal redirect keeping
    the filter."""

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
        # Owned AND streaming: owning wins, so it files under local only
        owned_streaming = make_movie("Filter Owned Streaming Film", 1989, tmdb_id=9400)
        make_movie_file(owned_streaming, "Bluray-1080p")
        streaming = make_movie("Filter Streaming Film", 1991, tmdb_id=9401)
        rentable = make_movie("Filter Rentable Film", 1992, tmdb_id=9402)
        warming = make_movie("Filter Warming Film", 1993, tmdb_id=9403)
        # Streaming AND rentable: the subscription wins over the rental
        both = make_movie("Filter Both Film", 1994, tmdb_id=9404)
        # Fetched, and carried by nobody the user subscribes to
        nowhere = make_movie("Filter Nowhere Film", 1995, tmdb_id=9405)
        # No TMDB id at all: known-negative, never pending
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
    # 9403 stays uncached: availability unknown, warming

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
    # The buckets partition the list: 2 + 2 + 1 + 2 = 7, plus the one
    # film still warming
    assert pill_counts(page) == {
        "all": 8,
        "local": 2,
        "services": 2,
        "rent": 1,
        "unavailable": 2,
    }
    assert "still being fetched" not in page

    local = admin_client.get("/watchlist?availability=local").get_data(as_text=True)
    assert "Filter Owned Film" in local
    assert "Filter Owned Streaming Film" in local
    assert "Filter Streaming Film" not in local
    assert "still being fetched" not in local

    services = admin_client.get("/watchlist?availability=services").get_data(
        as_text=True
    )
    assert "Filter Streaming Film" in services
    assert "Filter Both Film" in services
    assert "Filter Owned Film" not in services
    assert "Filter Owned Streaming Film" not in services
    assert "Filter Rentable Film" not in services
    assert "Filter Warming Film" not in services
    assert "1 film aren't shown here yet" in services

    rent = admin_client.get("/watchlist?availability=rent").get_data(as_text=True)
    assert "Filter Rentable Film" in rent
    assert "Filter Both Film" not in rent
    assert "Filter Owned Film" not in rent
    assert "Filter Nowhere Film" not in rent
    assert "still being fetched" in rent

    unavailable = admin_client.get("/watchlist?availability=unavailable").get_data(
        as_text=True
    )
    assert "Filter Nowhere Film" in unavailable
    assert "Filter Untracked Film" in unavailable
    assert "Filter Warming Film" not in unavailable
    assert "Filter Rentable Film" not in unavailable
    assert "Filter Owned Film" not in unavailable
    assert "still being fetched" in unavailable

    # Removal under a filter redirects back INTO the filter

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
    """The title search (#216) and duration filter (#195): both narrow
    the list before the availability pills count, match the landing
    page's runtime semantics (unknown runtimes hide only from filtered
    views), survive the removal redirect, and clear from one link."""

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

    # The title search matches the display title AND the TMDB title,
    # case-insensitively, and the pills count the narrowed set

    page = admin_client.get("/watchlist?q=BRISK").get_data(as_text=True)
    assert "Beta Brisk" in page
    assert "Brisk Renamed" in page
    assert "Alpha Epic" not in page
    assert "Gamma Mystery" not in page
    assert pill_counts(page)["all"] == 2
    assert ">Clear</a>" in page

    # The duration filter keeps films that fit, hides unknown runtimes,
    # says so, and captions each tile with its runtime

    page = admin_client.get("/watchlist?minutes=100").get_data(as_text=True)
    assert "Beta Brisk" in page
    assert "Brisk Renamed" in page
    assert "Alpha Epic" not in page
    assert "Gamma Mystery" not in page
    assert "films with unknown runtimes are hidden" in page
    assert "90 min" in page

    # The filters stack, and a nonsense minutes value is ignored

    page = admin_client.get("/watchlist?q=brisk&minutes=95").get_data(as_text=True)
    assert "Beta Brisk" in page
    assert "Brisk Renamed" not in page
    page = admin_client.get("/watchlist?minutes=0").get_data(as_text=True)
    assert pill_counts(page)["all"] == 4

    # A search with no matches offers the whole list back

    page = admin_client.get("/watchlist?q=zzzzzz").get_data(as_text=True)
    assert "No watchlisted films match this search." in page
    assert "Show all 4 watchlisted films" in page

    # Removal under the filters redirects back INTO them

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
    """The tile's Remove posts in the background (#187): the form wears
    data-card-watchlist plus the remove-cell marker, the cell carries
    its bucket for the pill bookkeeping, and the card-header post gets
    JSON back instead of a redirect."""

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
