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

    # A review with no rating anywhere stays unrated
    tall_t = films[("The Tall T", 1957)]
    assert tall_t["rating"] is None
    assert tall_t["entries"][0]["review"] == "A nice day."
    assert tall_t["entries"][0]["rating"] is None

    # Rated-only and liked-only films get a single dateless entry
    assert films[("Sharknado", 2013)]["entries"][0]["watched"] is None
    london = films[("The London Story", 1986)]
    assert london["liked"] is True
    assert london["entries"][0]["watched"] is None


def test_letterboxd_zip_upload_enqueues_match_task(app, admin_client):
    page = admin_client.get("/reviews").get_data(as_text=True)

    response = admin_client.post(
        "/reviews",
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

        assert second.date_watched == datetime(2024, 6, 1)
        assert second.rating == 5
        assert second.review == "Still holds up."
        assert second.date_reviewed == datetime(2024, 6, 2)
        assert second.liked is True

        # Re-importing the same export updates rather than duplicates
        assert apply_letterboxd_import(user_id, films) is True
        assert (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).count()
            == 2
        )

        # The reviews page renders unrated and liked entries
        page = admin_client.get("/reviews").get_data(as_text=True)
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

    page = admin_client.get("/reviews").get_data(as_text=True)
    response = admin_client.post(
        "/reviews",
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
        "Review",
    ]

    by_title = {row[2]: row for row in rows[1:]}
    jaws = by_title["Jaws"]
    assert jaws[0] == "578" and jaws[1] == "tt0073195"
    # Whole-number ratings export without a decimal; watched timestamps
    # truncate to the calendar date Letterboxd requires
    assert jaws[4] == "4"
    assert jaws[5] == "2024-06-01"
    assert 'He said "we need a bigger boat", or similar.' in jaws[6]

    tall_t = by_title["The Tall T"]
    assert tall_t[4] == "" and tall_t[5] == ""
    assert tall_t[6] == "A nice day."


def test_legacy_json_lines_upload_still_works(app, admin_client):
    page = admin_client.get("/reviews").get_data(as_text=True)

    legacy = b'{"name": "Old Import Film", "rating": 3.5}\n'
    response = admin_client.post(
        "/reviews",
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
