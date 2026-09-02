"""Test the Letterboxd review import and export.

These tests cover the parse of an account-export zip, the film match,
the load of the user_movie_review table, and the export of a CSV in the
Letterboxd import format."""

import inspect
import io
import re
import zipfile

from datetime import datetime

from tests.factories import make_movie


def assert_binds(job):
    """Test that the enqueued call matches the signature of the target
    function."""

    inspect.signature(job.func).bind(*(job.args or ()), **(job.kwargs or {}))


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def letterboxd_zip(files):
    """Build a Letterboxd export zip in memory from {name: csv_text}."""

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
    # The review row has the same watched date as the second diary entry.
    # Thus, Fitzflix merges them into 1 entry
    assert jaws["entries"][1]["review"] == "Still holds up."
    assert jaws["entries"][1]["rating"] == 5
    assert jaws["entries"][0]["review"] is None

    # Fitzflix stores the rewatch flag as given. A blank diary cell is a
    # first watch
    assert jaws["entries"][0]["rewatch"] is False
    assert jaws["entries"][1]["rewatch"] is True

    # A review with no rating in any file stays unrated
    tall_t = films[("The Tall T", 1957)]
    assert tall_t["rating"] is None
    assert tall_t["entries"][0]["review"] == "A nice day."
    assert tall_t["entries"][0]["rating"] is None

    # A rated-only film and a liked-only film get 1 entry without a date.
    # The rewatch state is unknown
    assert films[("Sharknado", 2013)]["entries"][0]["watched"] is None
    assert films[("Sharknado", 2013)]["entries"][0]["rewatch"] is None
    london = films[("The London Story", 1986)]
    assert london["liked"] is True
    assert london["entries"][0]["watched"] is None


def test_letterboxd_zip_upload_enqueues_match_task(app, admin_client):
    page = admin_client.get("/profile").get_data(as_text=True)

    response = admin_client.post(
        "/profile",
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
    """Test that the films in the library match without TMDB.

    With no API key configured, Fitzflix skips the unowned films. The
    import does not fail."""

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

        # A second import of the same export updates the rows. It does
        # not make duplicates
        assert apply_letterboxd_import(user_id, films) is True
        assert (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).count()
            == 2
        )

        # The reviews page renders the unrated entries and the liked
        # entries
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
                review="He said “we’re gonna need a bigger boat” \U0001f988, or similar.",
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

    import app.main.account as account

    def fake_send_email(subject, sender, recipients, **kwargs):
        sent["attachments"] = kwargs.get("attachments")

    monkeypatch.setattr(account, "send_email", fake_send_email)

    page = admin_client.get("/profile").get_data(as_text=True)
    response = admin_client.post(
        "/profile",
        data={
            "csrf_token": csrf_token_from(page),
            "export_submit": "Export Reviews",
        },
    )
    assert response.status_code == 302
    assert sent["attachments"]

    filename, mimetype, contents = sent["attachments"][0]
    # UTF-8 bytes, never str. The email package would apply
    # raw-unicode-escape to a str payload. That damages curly quotes and
    # emoji
    assert isinstance(contents, bytes)
    assert mimetype == "text/csv; charset=utf-8"
    rows = list(csv_module.reader(io.StringIO(contents.decode("utf-8"))))
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
    # A whole-number rating exports without a decimal. A watched timestamp
    # truncates to the calendar date that Letterboxd requires
    assert jaws[4] == "4"
    assert jaws[5] == "2024-06-01"
    # This row has a recorded rewatch flag. Thus, it exports as the spec
    # says
    assert jaws[6] == "Yes"
    assert "He said “we’re gonna need a bigger boat” \U0001f988, or similar." in jaws[7]

    tall_t = by_title["The Tall T"]
    assert tall_t[4] == "" and tall_t[5] == ""
    # A legacy row with no rewatch data exports a blank cell
    assert tall_t[6] == ""
    assert tall_t[7] == "A nice day."


def capture_sent_attachments(monkeypatch):
    """Stub the send_email of the History page.

    Return the list that collects the attachments of each call."""

    import app.main.account as account

    sent = []

    def fake_send_email(subject, sender, recipients, **kwargs):
        sent.append(kwargs.get("attachments"))

    monkeypatch.setattr(account, "send_email", fake_send_email)
    return sent


def export_reviews(client, token, full=False):
    """POST the export form of the Profile page (on Profile after #215)."""

    data = {"csrf_token": token, "export_submit": "Export Reviews"}
    if full:
        data["full_export"] = "y"
    return client.post("/profile", data=data)


def exported_titles(attachments):
    """Return the set of film titles in the CSV attachment of an export
    call."""

    import csv as csv_module

    filename, mimetype, contents = attachments[0]
    text = io.StringIO(contents.decode("utf-8"))
    return {row[2] for row in list(csv_module.reader(text))[1:]}


def test_incremental_export_covers_only_entries_since_last_export(
    app, admin_client, monkeypatch
):
    from datetime import timedelta

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user = User.query.first()
        # The app fixture is session-scoped. Thus, delete a baseline that
        # the export of an earlier test stamped
        user.date_reviews_exported = None
        user.last_export_review_id = None
        user_id = user.id
        first = make_movie("The First Watch", 2001)
        edited = make_movie("The Edited Review", 2002)
        edited_movie_id = edited.id
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=first.id,
                date_watched=datetime(2024, 1, 1),
                **star_rating_fields(3.0),
            )
        )
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=edited.id,
                review="First impressions.",
                date_watched=datetime(2024, 2, 1),
                date_reviewed=datetime(2024, 2, 1),
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()

    sent = capture_sent_attachments(monkeypatch)
    token = csrf_token_from(admin_client.get("/profile").get_data(as_text=True))

    # The first export has no baseline. Thus, the default covers all rows

    assert export_reviews(admin_client, token).status_code == 302
    assert sent[-1][0][0] == "reviews.csv"
    assert {"The First Watch", "The Edited Review"} <= exported_titles(sent[-1])

    with app.app_context():
        user = db.session.get(User, user_id)
        assert user.date_reviews_exported is not None
        assert user.last_export_review_id == (
            db.session.query(db.func.max(UserMovieReview.id))
            .filter(UserMovieReview.user_id == user_id)
            .scalar()
        )

        # A watch logged after the export, but with a date before it. Only
        # the row id shows that it is new

        backdated = make_movie("The Backdated Watch", 2003)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=backdated.id,
                date_watched=datetime(2020, 6, 1),
                **star_rating_fields(None),
            )
        )

        # An edit to a row that was exported before, stamped as review_edit
        # does

        row = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=edited_movie_id
        ).one()
        row.review = "On reflection, better."
        row.date_updated = datetime.now() + timedelta(minutes=1)
        db.session.commit()

    assert export_reviews(admin_client, token).status_code == 302
    filename = sent[-1][0][0]
    assert filename.startswith("reviews-since-")
    titles = exported_titles(sent[-1])
    assert "The Backdated Watch" in titles
    assert "The Edited Review" in titles
    assert "The First Watch" not in titles


def test_full_export_checkbox_exports_everything(app, admin_client, monkeypatch):
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user = User.query.first()
        user.date_reviews_exported = None
        user.last_export_review_id = None
        user_id = user.id
        for title, year in (("Old Faithful", 1990), ("Older Faithful", 1980)):
            db.session.add(
                UserMovieReview(
                    user_id=user_id,
                    movie_id=make_movie(title, year).id,
                    date_watched=datetime(2024, 3, 1),
                    **star_rating_fields(3.5),
                )
            )
        db.session.commit()

    sent = capture_sent_attachments(monkeypatch)
    token = csrf_token_from(admin_client.get("/profile").get_data(as_text=True))
    export_reviews(admin_client, token)

    # There is nothing new. But the checkbox exports all rows again

    assert export_reviews(admin_client, token, full=True).status_code == 302
    assert sent[-1][0][0] == "reviews.csv"
    assert {"Old Faithful", "Older Faithful"} <= exported_titles(sent[-1])


def test_incremental_export_with_nothing_new_sends_no_email(
    app, admin_client, monkeypatch
):
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user = User.query.first()
        user.date_reviews_exported = None
        user.last_export_review_id = None
        user_id = user.id
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=make_movie("A Single Watch", 2010).id,
                date_watched=datetime(2024, 4, 1),
                **star_rating_fields(2.5),
            )
        )
        db.session.commit()

    sent = capture_sent_attachments(monkeypatch)
    token = csrf_token_from(admin_client.get("/profile").get_data(as_text=True))
    export_reviews(admin_client, token)
    assert len(sent) == 1

    with app.app_context():
        baseline = db.session.get(User, user_id).date_reviews_exported

    response = admin_client.post(
        "/profile",
        data={"csrf_token": token, "export_submit": "Export Reviews"},
        follow_redirects=True,
    )
    assert "Nothing logged or updated since your last export" in response.get_data(
        as_text=True
    )
    assert len(sent) == 1

    # The empty export made no file. Thus, the baseline must not advance

    with app.app_context():
        assert db.session.get(User, user_id).date_reviews_exported == baseline


def test_legacy_json_lines_upload_still_works(app, admin_client):
    page = admin_client.get("/profile").get_data(as_text=True)

    legacy = b'{"name": "Old Import Film", "rating": 3.5}\n'
    response = admin_client.post(
        "/profile",
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
    """Test that a review without a star rating does not break the movie
    detail page.

    Such a review is possible after the Letterboxd import."""

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        # The review block renders only for the movies with a TMDB match
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

    # A liked-only viewing shows as UNRATED in the interface (rule set by
    # Glenn): an empty interactive row, no heart, no filled stars. The
    # like still feeds the profile as an imputed 3

    response = admin_client.get(f"/movie/{movie_id}")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "star-row" in page
    assert "bi-heart-fill" not in page
    assert "star filled" not in page and "star estimated" not in page


class FakeTMDBDetails:
    """Provide a canned TMDB movie-details response."""

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
            # Deep in the billing. The cast scroller shows every actor, not
            # only the top-billed 3
            {
                "id": 555444333,
                "name": "Deep Billed Player",
                "order": 9,
                "character": "Beach Extra",
            },
        ],
        # 2 credit rows for the same director (a common TMDB artifact)
        # collapse to 1 directed-by entry. Other crew never appear
        "crew": [
            {"id": 56512, "name": "Jeannot Szwarc", "job": "Director"},
            {"id": 56512, "name": "Jeannot Szwarc", "job": "Director"},
            {"id": 491, "name": "John Williams", "job": "Original Music Composer"},
        ],
    },
}


def test_movie_page_ladder_auto_flags_liked_at_three_stars(app, admin_client):
    """Test that 3 or more stars set the liked flag.

    This is the rule set by Glenn: liked means a positive verdict. Fewer
    than 3 stars do not set the flag. The tap defaults to a row without a
    date."""

    from app import db
    from app.models import User, UserMovieReview

    with app.app_context():
        user_id = User.query.first().id
        liked_film = make_movie("Auto Liked Film", 1994)
        meh_film = make_movie("Auto Meh Film", 1995)
        db.session.commit()
        liked_id, meh_id = liked_film.id, meh_film.id

    page = admin_client.get(f"/movie/{liked_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{liked_id}",
        data={"csrf_token": csrf_token_from(page), "quick_rating": "3"},
    )
    assert response.status_code == 302

    with app.app_context():
        review = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=liked_id
        ).one()
        assert review.liked is True
        assert float(review.rating) == 3.0
        assert review.whole_stars == 3
        assert review.date_watched is None

    page = admin_client.get(f"/movie/{meh_id}").get_data(as_text=True)
    admin_client.post(
        f"/movie/{meh_id}",
        data={"csrf_token": csrf_token_from(page), "quick_rating": "2"},
    )
    with app.app_context():
        review = UserMovieReview.query.filter_by(user_id=user_id, movie_id=meh_id).one()
        assert review.liked is False
        assert float(review.rating) == 2.0


def test_movie_page_logs_bare_watches(app, admin_client):
    """Test that a submission with no rating, like, or text is a plain
    diary entry.

    It is a watch, not a review. It has no review date. Fitzflix computes
    the rewatch flag in the same way as for a Plex watch."""

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
            "review_submit": "Log Movie",
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

    # A second log of the film is a rewatch

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    admin_client.post(
        f"/movie/{movie_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "review_submit": "Log Movie",
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
    import app.main.discover as discover

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        discover, "tmdb_get", lambda *a, **k: FakeTMDBDetails(JAWS_2_DETAILS)
    )

    from app import db
    from app.models import TMDBCredit

    with app.app_context():
        db.session.add(TMDBCredit(id=4430, name="Roy Scheider"))
        db.session.commit()

    page = admin_client.get("/review/tmdb/579").get_data(as_text=True)
    assert "Jaws 2 (1978)" in page
    assert "is not in the library" in page
    assert 'name="quick_rating"' in page
    # The runtime, the genres, and the US certification badge, as on the
    # movie page
    assert "116&nbsp;minutes" in page
    assert "Horror" in page and "Thriller" in page
    assert ">PG</span>" in page
    # The directed-by line, without duplicates and with a link to the
    # filmography page (#186). Crew in other jobs stay off the page
    assert page.count("Jeannot Szwarc") == 1
    assert "credit=56512" in page
    assert "John Williams" not in page
    # The log-a-viewing form (date + review text), as on the movie page.
    # The route has always handled both fields
    assert 'name="date_watched"' in page
    assert 'name="review"' in page
    assert "Log a viewing" in page
    # The watchlist toggle matches the live markup of the movie page.
    # Both faces render inside a data-watchlist-scope with the badge
    assert "data-card-watchlist" in page
    assert 'name="add_watchlist_submit"' in page
    assert 'name="remove_watchlist_submit"' in page
    assert "data-watchlist-badge" in page
    # The cast scroller shows every credited actor. Each actor links to a
    # filmography page. The page serves any TMDB person id. Thus, a
    # person without local credit rows browses the same as a known one.
    # The deep-billed names (order > 2) also appear
    assert "Roy Scheider" in page
    assert "credit=4430" in page
    assert "Unknown Costar" in page
    assert "credit=999888777" in page
    assert "Deep Billed Player" in page
    assert "Beach Extra" in page
    assert "cast-scroller" in page
    # The store-search dropdown and the external links render for the
    # film. But there is no Files button and no shopping-list toggle
    assert "blu-ray.com/movies/search.php" in page
    assert ">Amazon</a>" in page and ">eBay</a>" in page
    assert "imdb.com/title/tt0077766" in page
    assert "themoviedb.org/movie/579" in page
    assert "letterboxd.com/tmdb/579" in page
    assert re.search(r"/movie/\d+/files", page) is None
    assert "exclude_submit" not in page
    # No profile is stored. Thus, the engine stays quiet
    assert 'title="Estimated' not in page
    assert "Might interest you" not in page


def test_review_tmdb_shows_estimate_and_interest_marker(app, admin_client, monkeypatch):
    """Test that the page without a record shows the engine as the movie
    page does (#186).

    The TMDB-keyed overlay of the shared score source feeds the estimate
    of the ladder. The coarse scorer awards "Might interest you" against
    the marker bar."""

    import json

    import app.main.discover as discover

    from app.models import User

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        discover, "tmdb_get", lambda *a, **k: FakeTMDBDetails(JAWS_2_DETAILS)
    )

    with app.app_context():
        user_id = User.query.first().id
    profile = {
        "affinities": {
            "genre:27": {"class": "genre", "label": "Horror", "count": 4, "score": 0.8}
        },
        "movies": 5,
        "calibration": {"scores": [0.0, 0.5], "stars": [1.0, 5.0]},
    }
    app.redis.set(f"fitzflix:recs:profile:{user_id}", json.dumps(profile))
    app.redis.hset(f"fitzflix:recs:scores:tmdb:{user_id}", "579", 0.25)

    page = admin_client.get("/review/tmdb/579").get_data(as_text=True)
    assert "Estimated 3 for you" in page
    assert "Might interest you" in page


def test_review_tmdb_creates_movie_and_enqueues_refresh(app, admin_client, monkeypatch):
    from app.models import Movie, User, UserMovieReview

    import app.main.discover as discover

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        discover, "tmdb_get", lambda *a, **k: FakeTMDBDetails(JAWS_2_DETAILS)
    )

    page = admin_client.get("/review/tmdb/579").get_data(as_text=True)
    response = admin_client.post(
        "/review/tmdb/579",
        data={
            "csrf_token": csrf_token_from(page),
            "quick_rating": "4",
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
        assert float(review.rating) == 4.0
        assert review.whole_stars == 4 and review.half_stars == 0
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

        # The positive log prepares the "since you liked…" strip on the
        # movie page that the redirect goes to

        from app.elicitation import last_response

        state = last_response(app.redis, user_id)
        assert state["movie_id"] == movie.id
        assert state["positive"] is True


def test_review_tmdb_not_interested_creates_flagged_record(
    app, admin_client, monkeypatch
):
    """Test the ladder \u2715 on the TMDB log page (#45b).

    The standalone button left with #184. The \u2715 creates the record
    and flags it in 1 step. It makes no diary row. It clears a watchlist
    entry. Thus, the film leaves every recommendation surface."""

    import app.main.discover as discover

    from app.models import (
        Movie,
        User,
        UserMovieReview,
        UserMovieStatus,
        UserWatchlist,
    )

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        discover, "tmdb_get", lambda *a, **k: FakeTMDBDetails(JAWS_2_DETAILS)
    )

    page = admin_client.get("/review/tmdb/579").get_data(as_text=True)
    assert 'name="not_interested_submit"' not in page
    response = admin_client.post(
        "/review/tmdb/579",
        data={
            "csrf_token": csrf_token_from(page),
            "quick_rating": "0",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        movie = Movie.query.filter_by(tmdb_id=579).one()
        user_id = User.query.first().id
        assert (
            UserMovieStatus.query.filter_by(
                user_id=user_id, movie_id=movie.id, kind="not_interested"
            ).first()
            is not None
        )
        assert UserMovieReview.query.filter_by(movie_id=movie.id).count() == 0
        assert UserWatchlist.query.filter_by(movie_id=movie.id).count() == 0


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
    """Test that the ratings chart is 10 half-star buckets rendered locally.

    There is no Google Charts. No review title is serialized into the
    page JavaScript."""

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
    # 5 whole-star bins. Each bin includes the half-step below it. Thus,
    # the 1.5-star review bins as "about 2 stars"
    assert 'title="2 reviews rated about 4 stars"' in page
    assert 'title="1 review rated about 2 stars"' in page
    # The tallest bin fills the chart. An empty bin keeps a 1% baseline
    assert 'style="height: 100%;"' in page
    assert 'style="height: 50%;"' in page
    assert 'style="height: 1%;"' in page
    assert "3 ratings" in page
    # The review titles are no longer inline in the page JavaScript
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

    # A text-only save must not change the stars

    response = admin_client.post(
        f"/review/{row_id}/edit",
        data={
            "csrf_token": csrf_token_from(page),
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
        assert float(row.rating) == 3.0
        assert row.review == "On reflection, better than I thought."
        # The row had no review date. Thus, the text change set one (a
        # first review, not an update)
        assert row.date_reviewed is not None
        assert row.date_updated is None
        # Still 1 row: edited in place, not logged as a new viewing
        assert (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).count()
            == 1
        )

    # A ladder tap rates the viewing again in place. It sets the liked
    # flag

    page = admin_client.get(f"/review/{row_id}/edit").get_data(as_text=True)
    admin_client.post(
        f"/review/{row_id}/edit",
        data={
            "csrf_token": csrf_token_from(page),
            "date_watched": "2024-01-05",
            "review": "On reflection, better than I thought.",
            "quick_rating": "4",
        },
    )
    with app.app_context():
        db.session.expire_all()
        row = db.session.get(UserMovieReview, row_id)
        assert float(row.rating) == 4.0
        assert row.whole_stars == 4 and row.half_stars == 0
        assert row.liked is True


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
        assert row.rewatch is False  # the edit did not change it


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
    # An empty edit is permitted now. A bare watch is a valid diary entry
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
        # The original review date stays. The edit goes into date_updated
        assert row.date_reviewed == datetime(2024, 5, 2)
        assert row.date_updated is not None

    # The edit occurred years after the review date. Thus, the history
    # page shows both dates

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
    """Test that the history page is in order of watch date.

    A new unreviewed Plex watch comes before the older reviewed entries.
    A rating-only row without a date does not appear. Such rows are
    preference signals, not viewings."""

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
    assert fresh < old
    assert "Dateless Rating Film" not in page


def test_history_row_star_tap_preserves_date_and_text(app, admin_client):
    """Test the live star forms on the history rows (#58c).

    The forms post to review_edit. A star-only post never changes the
    date or the text of the viewing. The per-row text form never changes
    the stars or the date."""

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user = User.query.filter_by(admin=True).first()
        movie = make_movie("History Row Film", 1988)
        row = UserMovieReview(
            user_id=user.id,
            movie_id=movie.id,
            liked=True,
            date_watched=datetime(2021, 3, 14, 20, 0),
            review="Original text",
            date_reviewed=datetime(2021, 3, 15),
            **star_rating_fields(4.0),
        )
        db.session.add(row)
        db.session.commit()
        review_id = row.id

    page = admin_client.get("/history").get_data(as_text=True)
    assert 'data-ladder-live="1"' in page
    assert f"/review/{review_id}/edit" in page
    assert "Add review text" not in page and "Edit review text" in page
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    # A star-only tap (the exact payload of the row form) changes the
    # stars and nothing else

    response = admin_client.post(
        f"/review/{review_id}/edit",
        data={"csrf_token": token, "quick_rating": "2"},
        headers={"X-Requested-With": "ladder"},
    )
    assert response.get_json()["rating"] == 2.0
    with app.app_context():
        row = db.session.get(UserMovieReview, review_id)
        assert float(row.rating) == 2.0
        assert row.date_watched == datetime(2021, 3, 14, 20, 0)
        assert row.review == "Original text"

    # The exact payload of the text form changes the text and nothing
    # else

    response = admin_client.post(
        f"/review/{review_id}/edit",
        data={
            "csrf_token": token,
            "review": "Rewritten text",
            "review_submit": "y",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        row = db.session.get(UserMovieReview, review_id)
        assert row.review == "Rewritten text"
        assert float(row.rating) == 2.0
        assert row.date_watched == datetime(2021, 3, 14, 20, 0)


def test_review_edit_redirects_back_to_the_history_page_it_came_from(app, admin_client):
    """Test that the per-row forms on the history page carry their page
    number.

    Thus, a save goes back to that page, not to page 1. Without a page
    number, the redirect stays the plain history URL."""

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Paged Film", 1997)
        row = UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            date_watched=datetime(2024, 3, 1),
            **star_rating_fields(3.0),
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    page = admin_client.get(f"/review/{row_id}/edit?page=3").get_data(as_text=True)
    assert 'href="/history?page=3"' in page  # breadcrumb and Cancel

    response = admin_client.post(
        f"/review/{row_id}/edit?page=3",
        data={
            "csrf_token": csrf_token_from(page),
            "review": "Saved from page three.",
            "review_submit": "Save Review",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/history?page=3")

    response = admin_client.post(
        f"/review/{row_id}/edit",
        data={
            "csrf_token": csrf_token_from(page),
            "review": "Saved without a page.",
            "review_submit": "Save Review",
        },
    )
    assert response.headers["Location"].endswith("/history")


def test_feed_created_rows_never_export_back_to_letterboxd(
    app, admin_client, monkeypatch
):
    """Test that a row with a letterboxd_guid does not export.

    Such a row came FROM the feed. It is already on Letterboxd. An export
    would send a duplicate back."""

    import csv as csv_module

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        local = make_movie("Local Verdict", 1980)
        synced = make_movie("Synced From Feed", 1981)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=local.id,
                date_watched=datetime(2026, 8, 1),
                **star_rating_fields(4.0),
            )
        )
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=synced.id,
                date_watched=datetime(2026, 8, 2),
                letterboxd_guid="letterboxd-watch-99",
                **star_rating_fields(5.0),
            )
        )
        db.session.commit()

    sent = {}

    import app.main.account as account

    def fake_send_email(subject, sender, recipients, **kwargs):
        sent["attachments"] = kwargs.get("attachments")

    monkeypatch.setattr(account, "send_email", fake_send_email)

    page = admin_client.get("/profile").get_data(as_text=True)
    admin_client.post(
        "/profile",
        data={
            "csrf_token": csrf_token_from(page),
            "export_submit": "Export Reviews",
            "full_export": "y",
        },
    )
    _, _, contents = sent["attachments"][0]
    titles = [
        row[2] for row in csv_module.reader(io.StringIO(contents.decode("utf-8")))
    ][1:]
    assert "Local Verdict" in titles
    assert "Synced From Feed" not in titles


def test_history_previews_estimates_for_unrated_viewings(app, admin_client):
    """Test that an unrated viewing shows the estimate of the engine in
    its history ladder.

    An unrated viewing is a Plex watch or an unrated import. The estimate
    shows until real stars arrive. A rated row shows its verdict and no
    estimate."""

    import json

    from app import db
    from app.models import User, UserMovieReview
    from app.recommendations import PROFILE_KEY, SCORES_KEY
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        watched = make_movie("History Bare Watch", 1994)
        rated = make_movie("History Rated", 1995)
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=watched.id, date_watched=datetime.now()
            )
        )
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=rated.id,
                date_watched=datetime.now(),
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()
        watched_id = watched.id

    app.redis.set(
        SCORES_KEY.format(user_id=user_id), json.dumps({str(watched_id): 9.0})
    )
    app.redis.set(
        PROFILE_KEY.format(user_id=user_id),
        json.dumps(
            {
                "affinities": {},
                "movies": 3,
                "calibration": {
                    "scores": [0.0, 1.0, 2.0, 3.0],
                    "stars": [1.0, 2.0, 4.0, 4.5],
                },
            }
        ),
    )

    page = admin_client.get("/history").get_data(as_text=True)
    assert "Estimated 4.5 for you" in page
    assert page.count("star estimated") == 5
    assert page.count("estimated est-partial") == 1
    assert page.count("star filled") == 4


def test_clearing_stars_repaints_back_to_the_estimate(app, admin_client):
    """Test that a tap on the current rating of a history row clears the
    stars.

    The live repaint answers with the estimate of the engine. The row
    falls back to the guess and does not go blank. This is the
    universal-star-row rule, extended to logged but unrated viewings."""

    import json

    from app import db
    from app.models import User, UserMovieReview
    from app.recommendations import PROFILE_KEY, SCORES_KEY
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Clear To Estimate", 1996)
        review = UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            date_watched=datetime.now(),
            **star_rating_fields(3.0),
        )
        db.session.add(review)
        db.session.commit()
        movie_id, review_id = movie.id, review.id

    app.redis.set(SCORES_KEY.format(user_id=user_id), json.dumps({str(movie_id): 9.0}))
    app.redis.set(
        PROFILE_KEY.format(user_id=user_id),
        json.dumps(
            {
                "affinities": {},
                "movies": 3,
                "calibration": {
                    "scores": [0.0, 1.0, 2.0, 3.0],
                    "stars": [1.0, 2.0, 4.0, 4.5],
                },
            }
        ),
    )

    page = admin_client.get("/history").get_data(as_text=True)
    state = admin_client.post(
        f"/review/{review_id}/edit",
        data={"csrf_token": csrf_token_from(page), "quick_rating": "3"},
        headers={"X-Requested-With": "ladder"},
    ).get_json()
    assert state["rating"] is None
    assert state["estimated"] == 4.5


def test_movie_page_letterboxd_first_logging(app, admin_client):
    """Test that a linked Letterboxd account sends dated logging to
    Letterboxd.

    With a linked account, the movie page shows the deep-link button, and
    the local log form becomes a fallback. Without a linked account, the
    local form is the plain "Log a viewing" path, and no button
    renders."""

    from app import db
    from app.models import User

    with app.app_context():
        movie = make_movie("Letterboxd First Film", 1999, tmdb_id=4242)
        # The session-scoped database can arrive with a username from the
        # feed-sync tests. This test sets both states itself
        User.query.first().letterboxd_username = None
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Log it on Letterboxd" not in page
    assert "Log a viewing" in page

    with app.app_context():
        User.query.first().letterboxd_username = "glenn"
        db.session.commit()

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Log it on Letterboxd" in page
    assert 'href="https://letterboxd.com/tmdb/4242/"' in page
    assert "Letterboxd unavailable? Log a viewing here" in page

    with app.app_context():
        User.query.first().letterboxd_username = None
        db.session.commit()


def test_history_letterboxd_rows_edit_on_letterboxd(app, admin_client):
    """Test that a row from the feed shows read-only stars.

    Such a row points its edits at Letterboxd, because the sync would
    revert a local change. A row logged locally keeps its per-row
    editors."""

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        synced = make_movie("Feed Synced Film", 1981, tmdb_id=5151)
        local = make_movie("Locally Logged Film", 1982)
        db.session.add_all(
            [
                UserMovieReview(
                    user_id=user_id,
                    movie_id=synced.id,
                    letterboxd_guid="letterboxd-review-111",
                    review="From the feed.",
                    date_watched=datetime(2026, 8, 10),
                    date_reviewed=datetime(2026, 8, 10),
                    **star_rating_fields(4.0),
                ),
                UserMovieReview(
                    user_id=user_id,
                    movie_id=local.id,
                    review="Logged here.",
                    date_watched=datetime(2026, 8, 11),
                    date_reviewed=datetime(2026, 8, 11),
                    **star_rating_fields(3.0),
                ),
            ]
        )
        db.session.commit()
        synced_row_id = UserMovieReview.query.filter_by(movie_id=synced.id).one().id
        local_row_id = UserMovieReview.query.filter_by(movie_id=local.id).one().id

    page = admin_client.get("/history").get_data(as_text=True)
    assert "Synced from Letterboxd" in page
    assert 'href="https://letterboxd.com/tmdb/5151/"' in page
    assert f"/review/{synced_row_id}/edit" not in page
    assert f"/review/{local_row_id}/edit" in page


def test_review_edit_refuses_letterboxd_rows(app, admin_client):
    """Test that the per-viewing editor rejects a row from the feed.

    The next poll would put the Letterboxd verdict over a local edit."""

    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Guarded Feed Film", 1984, tmdb_id=6161)
        row = UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            letterboxd_guid="letterboxd-watch-222",
            review="",
            date_watched=datetime(2026, 8, 12),
            date_reviewed=datetime(2026, 8, 12),
            **star_rating_fields(4.0),
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    response = admin_client.get(f"/review/{row_id}/edit")
    assert response.status_code == 302
    assert "/history" in response.headers["Location"]

    # A feed-only diary renders no forms (the ladder is read-only, and
    # the import/export forms are on Profile now). Thus, the csrf token
    # of the session comes from a page that has one
    page = admin_client.get("/profile").get_data(as_text=True)
    response = admin_client.post(
        f"/review/{row_id}/edit",
        data={"csrf_token": csrf_token_from(page), "quick_rating": "1"},
    )
    assert response.status_code == 302

    with app.app_context():
        row = UserMovieReview.query.filter_by(id=row_id).one()
        assert float(row.rating) == 4.0


def _result(title, year):
    return {
        "title": title,
        "release_date": f"{year}-01-01" if year else "",
        "id": hash(title) % 10**6,
    }


def test_pick_tmdb_match_prefers_exact_title_in_year_results(app):
    """ "A Close Shave" (1995) must beat the making-of featurette.

    TMDB ranks the featurette first in the year-filtered results."""

    from app.videos import _pick_tmdb_match

    year_filtered = [
        _result('The Digital Special Effects in "A Close Shave"', 1995),
        _result("A Close Shave", 1996),
    ]
    picked = _pick_tmdb_match("A Close Shave", 1995, year_filtered, [])
    assert picked["title"] == "A Close Shave"


def test_pick_tmdb_match_falls_through_year_junk_to_exact_title(app):
    """ "300" (2006): the exact title from the title-only search wins.

    Every year-filtered result is a different film. Thus, the exact title
    wins, although its year is 2007. The match is never "My Poetic Works
    300 Yen"."""

    from app.videos import _pick_tmdb_match

    year_filtered = [_result("My Poetic Works 300 Yen", 2006)]
    title_only = [_result("300", 2007), _result("300: Rise of an Empire", 2014)]
    picked = _pick_tmdb_match("300", 2006, year_filtered, title_only)
    assert picked["title"] == "300"


def test_pick_tmdb_match_accepts_lone_exact_title_across_years(app):
    """The Men Who Tread on the Tiger's Tail: Letterboxd says 1945, TMDB
    says 1952.

    Fitzflix accepts a single exact-title match at any year distance."""

    from app.videos import _pick_tmdb_match

    title = "The Men Who Tread on the Tiger's Tail"
    title_only = [_result(title, 1952), _result("It Is Wonderful to Create", 2002)]
    picked = _pick_tmdb_match(title, 1945, [], title_only)
    assert picked["title"] == title


def test_pick_tmdb_match_nearest_year_among_exact_titles(app):
    """Casino Royale (1967) selects the 1967 spoof, not the 2006 film."""

    from app.videos import _pick_tmdb_match

    title_only = [
        _result("Casino Royale", 2006),
        _result("Casino Royale", 1967),
        _result("Casino Royale", 1954),
    ]
    picked = _pick_tmdb_match("Casino Royale", 1967, [], title_only)
    assert picked["release_date"].startswith("1967")


def test_pick_tmdb_match_keeps_alternative_title_head(app):
    """Waking Ned Devine matches the TMDB "Waking Ned" through an
    alternative title.

    The first year-filtered result stays the fallback."""

    from app.videos import _pick_tmdb_match

    year_filtered = [_result("Waking Ned", 1998)]
    picked = _pick_tmdb_match("Waking Ned Devine", 1998, year_filtered, [])
    assert picked["title"] == "Waking Ned"


def test_pick_tmdb_match_normalizes_dashes(app):
    """The en-dash Star Wars titles of Letterboxd equal the hyphens of
    TMDB."""

    from app.videos import _normalize_title, _pick_tmdb_match

    lb = "Star Wars: Episode I – The Phantom Menace"
    tmdb = "Star Wars: Episode I - The Phantom Menace"
    assert _normalize_title(lb) == _normalize_title(tmdb)
    picked = _pick_tmdb_match(lb, 1999, [_result(tmdb, 1999)], [])
    assert picked["title"] == tmdb


def test_pick_tmdb_match_skips_when_nothing_plausible(app):
    """TRIGUN (a TV series) matches nothing and stays unresolved."""

    from app.videos import _pick_tmdb_match

    assert (
        _pick_tmdb_match("TRIGUN", 1998, [], [_result("Trigun: Badlands Rumble", 2010)])
        is None
    )


def test_history_orders_by_day_then_time_then_title(app, admin_client):
    """Test the order inside a day (#196): later viewings first, then title.

    A midnight date_watched means "no time recorded". Only a watch logged
    on the day of the watch keeps a clock time (_watched_timestamp). A
    sort on the raw timestamp put every date-only row below any timed row
    on the same day. The log time of the row was not important. The time
    when Fitzflix wrote the row is the best evidence left. Thus, it
    replaces the missing clock time.
    """

    from app import db
    from app.models import User, UserMovieReview
    from tests.conftest import ADMIN_EMAIL

    with app.app_context():
        user_id = User.query.filter_by(email=ADMIN_EMAIL).one().id
        watched = datetime(2026, 3, 14)

        def log(title, watched_at, reviewed_at):
            movie = make_movie(title, 2001)
            db.session.add(
                UserMovieReview(
                    user_id=user_id,
                    movie_id=movie.id,
                    review="",
                    date_watched=watched_at,
                    date_reviewed=reviewed_at,
                )
            )

        # The same day: 1 watch with a real clock time, 1 date-only row
        # logged later that evening, and 2 date-only rows with no time.
        # The last 2 must fall back to the title
        log("Evening Entry", watched, datetime(2026, 3, 14, 23, 19))
        log(
            "Clocked Watch",
            watched.replace(hour=18, minute=55),
            datetime(2026, 3, 14, 19, 5),
        )
        log("Zebra Untimed", watched, watched)
        log("Alpha Untimed", watched, watched)

        # A different day, to show that the day is still the first key
        log(
            "Yesterday Film", datetime(2026, 3, 13, 22, 0), datetime(2026, 3, 13, 22, 5)
        )
        db.session.commit()

    page = admin_client.get("/history").get_data(as_text=True)
    order = sorted(
        (
            "Evening Entry",
            "Clocked Watch",
            "Zebra Untimed",
            "Alpha Untimed",
            "Yesterday Film",
        ),
        key=page.index,
    )
    assert order == [
        "Evening Entry",  # 23:19, latest known time that day
        "Clocked Watch",  # 18:55 on the clock
        "Alpha Untimed",  # no time at all -> title
        "Zebra Untimed",
        "Yesterday Film",  # the previous day, with any time
    ]


def test_history_forms_moved_per_215(app, admin_client):
    """Test that the History header carries only the title (#215).

    The Log a film box is above the diary rows, scoped to movies. The
    import and export forms are on the Profile page."""

    page = admin_client.get("/history").get_data(as_text=True)
    assert 'name="upload_submit"' not in page
    assert 'name="export_submit"' not in page
    assert 'name="scope" value="movies"' in page
    assert "Log a film" in page

    profile = admin_client.get("/profile").get_data(as_text=True)
    assert 'name="upload_submit"' in profile
    assert 'name="export_submit"' in profile
    assert "Review Import &amp; Export" in profile
