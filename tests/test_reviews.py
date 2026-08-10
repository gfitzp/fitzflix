"""Letterboxd review import and export: parsing an account-export zip,
matching films, loading the user_movie_review table, and exporting a CSV
in the Letterboxd import format."""

import inspect
import io
import re
import zipfile

from datetime import datetime

from tests.factories import make_movie


def assert_binds(job):
    """The enqueued call must match the target function's signature."""

    inspect.signature(job.func).bind(*(job.args or ()), **(job.kwargs or {}))


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def letterboxd_zip(files):
    """Build an in-memory Letterboxd export zip from {name: csv_text}."""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


SAMPLE_EXPORT = {
    "ratings.csv": (
        "Date,Name,Year,Letterboxd URI,Rating\n"
        "2023-09-08,Jaws,1975,https://boxd.it/aaa,4.5\n"
        "2023-09-08,Sharknado,2013,https://boxd.it/bbb,1\n"
    ),
    "diary.csv": (
        "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
        "2023-09-08,Jaws,1975,https://boxd.it/ccc,4,,,2015-01-09\n"
        "2024-06-02,Jaws,1975,https://boxd.it/ddd,5,Yes,,2024-06-01\n"
    ),
    "reviews.csv": (
        "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date\n"
        '2024-06-02,Jaws,1975,https://boxd.it/ddd,5,Yes,"Still holds up.",,2024-06-01\n'
        '2024-01-07,The Tall T,1957,https://boxd.it/eee,,,"A nice day.",,2024-01-06\n'
    ),
    "likes/films.csv": (
        "Date,Name,Year,Letterboxd URI\n"
        "2023-11-29,Jaws,1975,https://boxd.it/aaa\n"
        "2023-11-29,The London Story,1986,https://boxd.it/fff\n"
    ),
}


def test_parse_letterboxd_export_merges_per_film(app):
    from app.videos import parse_letterboxd_export

    films = {
        (f["title"], f["year"]): f
        for f in parse_letterboxd_export(letterboxd_zip(SAMPLE_EXPORT))
    }

    jaws = films[("Jaws", 1975)]
    assert jaws["rating"] == 4.5
    assert jaws["liked"] is True
    assert [e["watched"] for e in jaws["entries"]] == ["2015-01-09", "2024-06-01"]
    # The review row shares a watched date with the second diary entry, so
    # they merge into one entry
    assert jaws["entries"][1]["review"] == "Still holds up."
    assert jaws["entries"][1]["rating"] == 5
    assert jaws["entries"][0]["review"] is None

    # Rewatch is stored as stated: a blank diary cell is a first watch
    assert jaws["entries"][0]["rewatch"] is False
    assert jaws["entries"][1]["rewatch"] is True

    # A review with no rating anywhere stays unrated
    tall_t = films[("The Tall T", 1957)]
    assert tall_t["rating"] is None
    assert tall_t["entries"][0]["review"] == "A nice day."
    assert tall_t["entries"][0]["rating"] is None

    # Rated-only and liked-only films get a single dateless entry, with an
    # unknown rewatch state
    assert films[("Sharknado", 2013)]["entries"][0]["watched"] is None
    assert films[("Sharknado", 2013)]["entries"][0]["rewatch"] is None
    london = films[("The London Story", 1986)]
    assert london["liked"] is True
    assert london["entries"][0]["watched"] is None


def test_letterboxd_zip_upload_enqueues_match_task(app, admin_client):
    page = admin_client.get("/history").get_data(as_text=True)

    response = admin_client.post(
        "/history",
        data={
            "csrf_token": csrf_token_from(page),
            "upload_submit": "Import Reviews",
            "file": (io.BytesIO(letterboxd_zip(SAMPLE_EXPORT)), "letterboxd.zip"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    jobs = [
        job
        for job in app.request_queue.jobs
        if job.func_name == "app.videos.letterboxd_import_task"
    ]
    assert len(jobs) == 1
    user_id, films = jobs[0].args
    assert {f["title"] for f in films} == {
        "Jaws",
        "Sharknado",
        "The Tall T",
        "The London Story",
    }
    assert_binds(jobs[0])


def test_match_task_resolves_library_films_and_hands_off(app):
    """Films already in the library match without TMDb; with no API key
    configured, unowned films are skipped rather than failing the import."""

    from app import db
    from app.videos import letterboxd_import_task, parse_letterboxd_export

    with app.app_context():
        movie = make_movie("Jaws", 1975)
        db.session.commit()
        movie_id = movie.id

        films = parse_letterboxd_export(letterboxd_zip(SAMPLE_EXPORT))
        assert letterboxd_import_task(999, films) is True

        jobs = [
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.apply_letterboxd_import"
        ]
        assert len(jobs) == 1
        user_id, resolved = jobs[0].args
        assert user_id == 999
        assert [f["title"] for f in resolved] == ["Jaws"]
        assert resolved[0]["movie_id"] == movie_id
        assert_binds(jobs[0])


def test_apply_letterboxd_import_loads_review_table(app, admin_client):
    from app import db
    from app.models import UserMovieReview
    from app.videos import apply_letterboxd_import, parse_letterboxd_export

    with app.app_context():
        movie = make_movie("Jaws", 1975)
        db.session.commit()
        movie_id = movie.id

        films = [
            f
            for f in parse_letterboxd_export(letterboxd_zip(SAMPLE_EXPORT))
            if f["title"] == "Jaws"
        ]
        films[0]["movie_id"] = movie_id

        from app.models import User

        user_id = User.query.first().id

        assert apply_letterboxd_import(user_id, films) is True

        reviews = (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id)
            .order_by(UserMovieReview.date_watched.asc())
            .all()
        )
        assert len(reviews) == 2

        first, second = reviews
        assert first.date_watched == datetime(2015, 1, 9)
        assert first.rating == 4
        assert first.whole_stars == 4 and first.half_stars == 0
        assert first.liked is True
        assert first.rewatch is False

        assert second.date_watched == datetime(2024, 6, 1)
        assert second.rating == 5
        assert second.review == "Still holds up."
        assert second.date_reviewed == datetime(2024, 6, 2)
        assert second.liked is True
        assert second.rewatch is True

        # Re-importing the same export updates rather than duplicates
        assert apply_letterboxd_import(user_id, films) is True
        assert (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).count()
            == 2
        )

        # The reviews page renders unrated and liked entries
        page = admin_client.get("/history").get_data(as_text=True)
        assert page and "bi-heart-fill" in page


def test_apply_creates_missing_movie_and_enqueues_refresh(app):
    from app.models import Movie, UserMovieReview
    from app.videos import apply_letterboxd_import

    with app.app_context():
        film = {
            "title": "The London Story",
            "year": 1986,
            "rating": None,
            "liked": True,
            "tmdb_id": 55555,
            "canonical_title": "The London Story",
            "canonical_year": 1986,
            "entries": [
                {"watched": None, "logged": None, "rating": None, "review": None}
            ],
        }
        assert apply_letterboxd_import(7, [film]) is True

        movie = Movie.query.filter_by(tmdb_id=55555).first()
        assert movie is not None
        assert movie.title == "The London Story" and movie.year == 1986

        review = UserMovieReview.query.filter_by(user_id=7, movie_id=movie.id).one()
        assert review.liked is True
        assert review.rating is None
        assert review.whole_stars is None

        refresh_jobs = [
            job
            for job in app.request_queue.jobs
            if job.func_name == "app.videos.refresh_tmdb_info"
            and job.args[1] == movie.id
        ]
        assert len(refresh_jobs) == 1


def test_review_export_uses_letterboxd_import_format(app, admin_client, monkeypatch):
    import csv as csv_module

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        rated = make_movie("Jaws", 1975, tmdb_id=578, imdb_id="tt0073195")
        unrated = make_movie("The Tall T", 1957)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=rated.id,
                review='He said "we need a bigger boat", or similar.',
                date_watched=datetime(2024, 6, 1, 20, 30),
                date_reviewed=datetime(2024, 6, 2),
                liked=True,
                rewatch=True,
                **star_rating_fields(4.0),
            )
        )
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=unrated.id,
                review="A nice day.",
                date_watched=None,
                date_reviewed=None,
                **star_rating_fields(None),
            )
        )
        db.session.commit()

    sent = {}

    import app.main.routes as main_routes

    def fake_send_email(subject, sender, recipients, **kwargs):
        sent["attachments"] = kwargs.get("attachments")

    monkeypatch.setattr(main_routes, "send_email", fake_send_email)

    page = admin_client.get("/history").get_data(as_text=True)
    response = admin_client.post(
        "/history",
        data={
            "csrf_token": csrf_token_from(page),
            "export_submit": "Export Reviews",
        },
    )
    assert response.status_code == 302
    assert sent["attachments"]

    filename, mimetype, contents = sent["attachments"][0]
    rows = list(csv_module.reader(io.StringIO(contents)))
    assert rows[0] == [
        "tmdbID",
        "imdbID",
        "Title",
        "Year",
        "Rating",
        "WatchedDate",
        "Rewatch",
        "Review",
    ]

    by_title = {row[2]: row for row in rows[1:]}
    jaws = by_title["Jaws"]
    assert jaws[0] == "578" and jaws[1] == "tt0073195"
    # Whole-number ratings export without a decimal; watched timestamps
    # truncate to the calendar date Letterboxd requires
    assert jaws[4] == "4"
    assert jaws[5] == "2024-06-01"
    # Rewatch was recorded on this row, so it exports per the spec
    assert jaws[6] == "Yes"
    assert 'He said "we need a bigger boat", or similar.' in jaws[7]

    tall_t = by_title["The Tall T"]
    assert tall_t[4] == "" and tall_t[5] == ""
    # A legacy row with no rewatch information exports a blank cell
    assert tall_t[6] == ""
    assert tall_t[7] == "A nice day."


def test_legacy_json_lines_upload_still_works(app, admin_client):
    page = admin_client.get("/history").get_data(as_text=True)

    legacy = b'{"name": "Old Import Film", "rating": 3.5}\n'
    response = admin_client.post(
        "/history",
        data={
            "csrf_token": csrf_token_from(page),
            "upload_submit": "Import Reviews",
            "file": (io.BytesIO(legacy), "ratings.json"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    jobs = [
        job for job in app.sql_queue.jobs if job.func_name == "app.videos.review_task"
    ]
    assert any(job.args[1] == "Old Import Film" for job in jobs)


def test_movie_page_renders_unrated_liked_review(app, admin_client):
    """A review without a star rating (possible since the Letterboxd
    import) must not break the movie detail page."""

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        # The review block only renders for TMDb-matched movies
        movie = make_movie(
            "Liked but Unrated", 1986, tmdb_data_as_of=datetime(2026, 1, 1)
        )
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=movie.id,
                review="",
                liked=True,
                **star_rating_fields(None),
            )
        )
        db.session.commit()
        movie_id = movie.id

    response = admin_client.get(f"/movie/{movie_id}")
    assert response.status_code == 200
    assert b"bi-heart-fill" in response.data
    assert b"bi-star-fill" not in response.data


class FakeTMDbDetails:
    """A canned TMDb movie-details response."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


JAWS_2_DETAILS = {
    "id": 579,
    "title": "Jaws 2",
    "release_date": "1978-06-16",
    "overview": "The shark is back.",
    "poster_path": "/jaws2.jpg",
    "imdb_id": "tt0077766",
    "runtime": 116,
    "genres": [{"id": 27, "name": "Horror"}, {"id": 53, "name": "Thriller"}],
    "release_dates": {
        "results": [
            {
                "iso_3166_1": "US",
                "release_dates": [{"certification": "PG"}],
            }
        ]
    },
    "credits": {
        "cast": [
            {
                "id": 4430,
                "name": "Roy Scheider",
                "order": 0,
                "profile_path": "/scheider.jpg",
            },
            {"id": 999888777, "name": "Unknown Costar", "order": 1},
            # Deep in the billing: the cast scroller shows everyone, not
            # just the top-billed three
            {
                "id": 555444333,
                "name": "Deep Billed Player",
                "order": 9,
                "character": "Beach Extra",
            },
        ]
    },
}


def test_movie_page_review_accepts_like_without_rating(app, admin_client):
    from app import db
    from app.models import User, UserMovieReview

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Like Only Film", 1994)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "review_submit": "Rate Movie",
            "rating": "",
            "liked": "y",
            "review": "",
            "date_watched": "",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        review = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=movie_id
        ).one()
        assert review.liked is True
        assert review.rating is None
        assert review.whole_stars is None


def test_movie_page_logs_bare_watches(app, admin_client):
    """A submission with no rating, like, or text is a plain diary entry:
    a watch, not a review — no review date, rewatch computed like a Plex
    watch would."""

    from app import db
    from app.models import UserMovieReview

    with app.app_context():
        movie = make_movie("Bare Watch Film", 1995)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "review_submit": "Rate Movie",
            "rating": "",
            "review": "",
            "date_watched": "2026-08-01",
        },
        follow_redirects=True,
    )
    assert "in your history" in response.get_data(as_text=True)

    with app.app_context():
        row = UserMovieReview.query.filter_by(movie_id=movie_id).one()
        assert row.rating is None
        assert row.liked is False
        assert row.review == ""
        assert row.date_reviewed is None
        assert row.rewatch is False

    # Logging the film again is a rewatch

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    admin_client.post(
        f"/movie/{movie_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "review_submit": "Rate Movie",
            "rating": "",
            "review": "",
            "date_watched": "2026-08-09",
        },
    )
    with app.app_context():
        rows = (
            UserMovieReview.query.filter_by(movie_id=movie_id)
            .order_by(UserMovieReview.date_watched.asc())
            .all()
        )
        assert [row.rewatch for row in rows] == [False, True]


def test_review_tmdb_renders_form_for_unowned_film(app, admin_client, monkeypatch):
    import app.main.routes as main_routes

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        main_routes, "tmdb_get", lambda *a, **k: FakeTMDbDetails(JAWS_2_DETAILS)
    )

    from app import db
    from app.models import TMDBCredit

    with app.app_context():
        db.session.add(TMDBCredit(id=4430, name="Roy Scheider"))
        db.session.commit()

    page = admin_client.get("/review/tmdb/579").get_data(as_text=True)
    assert "Jaws 2 (1978)" in page
    assert "isn&#39;t in the library" in page or "isn't in the library" in page
    assert 'name="liked"' in page
    # Runtime, genres, and the US certification badge, like the movie page
    assert "116&nbsp;minutes" in page
    assert "Horror" in page and "Thriller" in page
    assert ">PG</span>" in page
    # The cast scroller shows every credited actor: locally known people
    # link to their filmography, others render unlinked, and deep-billed
    # names (order > 2) appear too
    assert "Roy Scheider" in page
    assert "credit=4430" in page
    assert "Unknown Costar" in page
    assert "credit=999888777" not in page
    assert "Deep Billed Player" in page
    assert "Beach Extra" in page
    assert "cast-scroller" in page
    # The store-search dropdown and external links render for the film,
    # but there's no Files button or shopping-list toggle
    assert "blu-ray.com/movies/search.php" in page
    assert ">Amazon</a>" in page and ">eBay</a>" in page
    assert "imdb.com/title/tt0077766" in page
    assert "themoviedb.org/movie/579" in page
    assert "letterboxd.com/tmdb/579" in page
    assert re.search(r"/movie/\d+/files", page) is None
    assert "exclude_submit" not in page


def test_review_tmdb_creates_movie_and_enqueues_refresh(app, admin_client, monkeypatch):
    from app.models import Movie, User, UserMovieReview

    import app.main.routes as main_routes

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        main_routes, "tmdb_get", lambda *a, **k: FakeTMDbDetails(JAWS_2_DETAILS)
    )

    page = admin_client.get("/review/tmdb/579").get_data(as_text=True)
    response = admin_client.post(
        "/review/tmdb/579",
        data={
            "csrf_token": csrf_token_from(page),
            "review_submit": "Rate Movie",
            "rating": "3.5",
            "liked": "y",
            "review": "Still a decent shark.",
            "date_watched": "2026-08-01",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        movie = Movie.query.filter_by(tmdb_id=579).one()
        assert movie.title == "Jaws 2" and movie.year == 1978
        assert response.headers["Location"].endswith(f"/movie/{movie.id}")

        user_id = User.query.first().id
        review = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=movie.id
        ).one()
        assert review.rating == 3.5
        assert review.whole_stars == 3 and review.half_stars == 1
        assert review.liked is True
        assert review.review == "Still a decent shark."
        assert review.date_watched == datetime(2026, 8, 1)

        refresh_jobs = [
            job
            for job in app.request_queue.jobs
            if job.func_name == "app.videos.refresh_tmdb_info"
            and job.args[1] == movie.id
        ]
        assert len(refresh_jobs) == 1


def test_review_tmdb_redirects_when_film_in_library(app, admin_client):
    from app import db

    with app.app_context():
        movie = make_movie("Already Here", 1980, tmdb_id=8888)
        db.session.commit()
        movie_id = movie.id

    response = admin_client.get("/review/tmdb/8888")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/movie/{movie_id}")


def test_reviews_page_renders_local_rating_distribution(app, admin_client):
    """The ratings chart is ten locally rendered half-star buckets — no
    Google Charts, no review titles serialized into page JavaScript."""

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        for title, year, rating, liked in (
            ("Distribution One", 1971, 4.0, True),
            ("Distribution Two", 1972, 4.0, False),
            ("Distribution Three", 1973, 1.5, False),
        ):
            movie = make_movie(title, year)
            db.session.add(
                UserMovieReview(
                    user_id=user_id,
                    movie_id=movie.id,
                    review="",
                    liked=liked,
                    **star_rating_fields(rating),
                )
            )
        db.session.commit()

    page = admin_client.get("/history").get_data(as_text=True)
    assert "charts/loader.js" not in page
    assert 'id="rating-distribution"' in page
    # Five whole-star bins, each absorbing the half-step below it: the
    # 1.5-star review bins as "about 2 stars"
    assert 'title="2 reviews rated about 4 stars"' in page
    assert 'title="1 review rated about 2 stars"' in page
    # The tallest bin fills the chart; empty bins keep a 1% baseline
    assert 'style="height: 100%;"' in page
    assert 'style="height: 50%;"' in page
    assert 'style="height: 1%;"' in page
    assert "3 ratings" in page
    # Review titles are no longer inlined into the page's JavaScript
    assert "arrayToDataTable" not in page


def test_history_page_offers_per_viewing_edit_links(app, admin_client):
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("History Film", 1990)
        reviewed = UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            review="Thoughts were had.",
            date_watched=datetime(2024, 3, 1),
            **star_rating_fields(3.5),
        )
        bare = UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            review="",
            date_watched=datetime(2024, 4, 1),
            **star_rating_fields(None),
        )
        db.session.add_all([reviewed, bare])
        db.session.commit()
        reviewed_id, bare_id = reviewed.id, bare.id

    page = admin_client.get("/history").get_data(as_text=True)
    assert "My History" in page
    assert f"/review/{reviewed_id}/edit" in page
    assert f"/review/{bare_id}/edit" in page
    assert "Edit review" in page
    assert "Add review" in page


def test_review_edit_updates_the_viewing_in_place(app, admin_client):
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Editable Film", 1995)
        row = UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            review="First impressions.",
            date_watched=datetime(2024, 1, 5),
            **star_rating_fields(3.0),
        )
        db.session.add(row)
        db.session.commit()
        row_id, movie_id = row.id, movie.id

    page = admin_client.get(f"/review/{row_id}/edit").get_data(as_text=True)
    assert "First impressions." in page
    assert 'value="2024-01-05"' in page

    response = admin_client.post(
        f"/review/{row_id}/edit",
        data={
            "csrf_token": csrf_token_from(page),
            "rating": "4.5",
            "liked": "y",
            "date_watched": "2024-01-05",
            "review": "On reflection, better than I thought.",
            "review_submit": "Save Review",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/history")

    with app.app_context():
        db.session.expire_all()
        row = db.session.get(UserMovieReview, row_id)
        assert row.rating == 4.5
        assert row.whole_stars == 4 and row.half_stars == 1
        assert row.liked is True
        assert row.review == "On reflection, better than I thought."
        # The row had no review date, so the text change set one (a first
        # review, not an update)
        assert row.date_reviewed is not None
        assert row.date_updated is None
        # Still one row: edited in place, not logged as a new viewing
        assert (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).count()
            == 1
        )


def test_review_edit_adds_review_to_bare_plex_viewing(app, admin_client):
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Plex Watched Film", 2001)
        row = UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            review="",
            date_watched=datetime(2026, 8, 1),
            rewatch=False,
            **star_rating_fields(None),
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    page = admin_client.get(f"/review/{row_id}/edit").get_data(as_text=True)
    response = admin_client.post(
        f"/review/{row_id}/edit",
        data={
            "csrf_token": csrf_token_from(page),
            "rating": "",
            "date_watched": "2026-08-01",
            "review": "Watched on Plex, loved it.",
            "review_submit": "Save Review",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        db.session.expire_all()
        row = db.session.get(UserMovieReview, row_id)
        assert row.review == "Watched on Plex, loved it."
        assert row.rating is None
        assert row.rewatch is False  # untouched by the edit


def test_review_edit_accepts_clearing_to_a_bare_watch(app, admin_client):
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Cleared Film", 2002)
        row = UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            review="",
            date_watched=datetime(2026, 8, 2),
            **star_rating_fields(None),
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    page = admin_client.get(f"/review/{row_id}/edit").get_data(as_text=True)
    response = admin_client.post(
        f"/review/{row_id}/edit",
        data={
            "csrf_token": csrf_token_from(page),
            "rating": "",
            "date_watched": "2026-08-02",
            "review": "",
            "review_submit": "Save Review",
        },
    )
    # An empty edit is legal now: a bare watch is a valid diary entry
    assert response.status_code == 302
    with app.app_context():
        row = db.session.get(UserMovieReview, row_id)
        assert row.rating is None
        assert row.liked is False
        assert row.review == ""


def test_review_edit_is_owner_only(app, admin_client, user_client):
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        admin_id = User.query.filter_by(admin=True).first().id
        movie = make_movie("Private Film", 2003)
        row = UserMovieReview(
            user_id=admin_id,
            movie_id=movie.id,
            review="Mine.",
            date_watched=datetime(2026, 8, 3),
            **star_rating_fields(4.0),
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    assert user_client.get(f"/review/{row_id}/edit").status_code == 404
    assert admin_client.get(f"/review/{row_id}/edit").status_code == 200


def test_review_edit_keeps_review_date_and_stamps_updated(app, admin_client):
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Revised Film", 2004)
        row = UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            review="Original thoughts.",
            date_watched=datetime(2024, 5, 1),
            date_reviewed=datetime(2024, 5, 2),
            **star_rating_fields(3.0),
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    page = admin_client.get(f"/review/{row_id}/edit").get_data(as_text=True)
    response = admin_client.post(
        f"/review/{row_id}/edit",
        data={
            "csrf_token": csrf_token_from(page),
            "rating": "3",
            "date_watched": "2024-05-01",
            "review": "Revised thoughts.",
            "review_submit": "Save Review",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        db.session.expire_all()
        row = db.session.get(UserMovieReview, row_id)
        assert row.review == "Revised thoughts."
        # The original review date survives; the edit lands in date_updated
        assert row.date_reviewed == datetime(2024, 5, 2)
        assert row.date_updated is not None

    # The edit happened years after the review date, so the history page
    # shows both dates

    page = admin_client.get("/history").get_data(as_text=True)
    assert "; updated" in page


def test_history_hides_update_date_from_the_same_day(app, admin_client):
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Same Day Film", 2005)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=movie.id,
                review="Reviewed and touched up within the day.",
                date_watched=datetime(2024, 7, 1),
                date_reviewed=datetime(2024, 7, 2, 9, 0),
                date_updated=datetime(2024, 7, 2, 22, 30),
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()

    page = admin_client.get("/history").get_data(as_text=True)
    assert "Reviewed and touched up within the day." in page
    assert "; updated" not in page


def test_history_orders_by_watch_date_with_unreviewed_on_top(app, admin_client):
    """The history page is chronological by watch date: a fresh unreviewed
    Plex watch outranks older reviewed entries, and dateless rating-only
    rows trail the dated history."""

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        old_reviewed = make_movie("Old Reviewed Film", 1970)
        fresh_watch = make_movie("Fresh Plex Watch Film", 1971)
        dateless = make_movie("Dateless Rating Film", 1972)
        db.session.add_all(
            [
                UserMovieReview(
                    user_id=user_id,
                    movie_id=old_reviewed.id,
                    review="Reviewed long ago.",
                    date_watched=datetime(2020, 1, 1),
                    date_reviewed=datetime(2026, 8, 1),
                    **star_rating_fields(4.0),
                ),
                UserMovieReview(
                    user_id=user_id,
                    movie_id=fresh_watch.id,
                    review="",
                    date_watched=datetime(2026, 8, 9),
                    rewatch=False,
                    **star_rating_fields(None),
                ),
                UserMovieReview(
                    user_id=user_id,
                    movie_id=dateless.id,
                    review="",
                    date_watched=None,
                    **star_rating_fields(3.0),
                ),
            ]
        )
        db.session.commit()

    page = admin_client.get("/history").get_data(as_text=True)
    fresh = page.index("Fresh Plex Watch Film")
    old = page.index("Old Reviewed Film")
    dateless_pos = page.index("Dateless Rating Film")
    assert fresh < old < dateless_pos
