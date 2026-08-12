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

    response = admin_client.post(
        "/watchlist",
        data={
            "csrf_token": csrf_token_from(page),
            "movie_id": str(movie_id),
            "remove_watchlist_submit": "Remove from Watchlist",
        },
    )
    assert response.status_code == 302
    assert entries_for(app, user_id) == [owned_id]

    response = admin_client.post(
        "/watchlist",
        data={
            "csrf_token": csrf_token_from(page),
            "movie_id": str(owned_id),
            "remove_watchlist_submit": "Remove from Watchlist",
        },
    )
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
    )
    assert response.status_code == 302

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
    assert "On your watchlist" in body

    # The watchlisted film pins ahead of the higher-scoring one

    assert body.index("Rail Wanted (1994)") < body.index("Rail Unwanted High (1994)")


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
    assert "On your watchlist" in body
    assert body.index("Library Wanted (1995)") < body.index(
        "Library Unwanted High (1994)"
    )
