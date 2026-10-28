"""Test that the paginated people and History queries have a unique tiebreak (#459).

Equal sort keys can change order between 2 LIMIT/OFFSET pages on
MariaDB. Then 1 row shows 2 times and its twin never."""

from datetime import datetime

from app import db
from tests.factories import make_movie, make_person


def _order_columns(query):
    """Return the ORDER BY columns of a query as compiled SQL text."""

    sql = str(query.statement.compile(compile_kwargs={"literal_binds": True}))
    return sql.rsplit("ORDER BY", 1)[1]


def test_the_people_search_ends_its_order_on_the_credit_id(app):
    from app.main.library import _ranked_people_query

    with app.app_context():
        query = _ranked_people_query("cast", "john", 1)
        assert _order_columns(query).strip().endswith("tmdb_credit.id ASC")


def test_two_namesakes_keep_one_order_across_pages(app, admin_client):
    """2 people with the same name and count list each 1 time on the search."""

    with app.app_context():
        film = make_movie("Namesake Film", 2001)
        make_person(459002, "John Bishop", [film])
        make_person(459001, "John Bishop", [film])
        db.session.commit()

    first = admin_client.get("/people?q=John+Bishop").get_data(as_text=True)
    assert first.count("credit=459001") >= 1
    assert first.count("credit=459002") >= 1
    assert first.index("credit=459001") < first.index("credit=459002")


def test_history_pages_split_a_tie_by_the_review_id(app, admin_client):
    """2 rows equal on every sort key straddle the page boundary. Each shows 1 time."""

    from flask import url_for

    from app.models import User, UserMovieReview

    with app.app_context():
        user = User.query.filter_by(admin=True).first()
        tied_film = make_movie("Aaa Tied Film", 2000)
        tied = []
        for _ in range(2):
            review = UserMovieReview(
                user_id=user.id,
                movie_id=tied_film.id,
                review="",
                date_watched=datetime(2020, 1, 1),
                date_reviewed=datetime(2020, 1, 1, 12, 0, 0),
            )
            db.session.add(review)
            db.session.flush()
            tied.append(review.id)
        for index in range(49):
            film = make_movie(f"Later Film {index:02d}", 2001)
            db.session.add(
                UserMovieReview(
                    user_id=user.id,
                    movie_id=film.id,
                    review="",
                    date_watched=datetime(2021, 1, 1 + index % 28),
                    date_reviewed=datetime(2021, 2, 1, 12, 0, index),
                )
            )
        db.session.commit()

    with app.test_request_context():
        edit = {
            review_id: url_for("main.review_edit", review_id=review_id, page=1)
            for review_id in tied
        }
    first = admin_client.get("/history").get_data(as_text=True)
    second = admin_client.get("/history?page=2").get_data(as_text=True)
    newer, older = max(tied), min(tied)
    assert edit[newer] in first
    assert edit[older] not in first
    assert edit[older].replace("page=1", "page=2") in second
