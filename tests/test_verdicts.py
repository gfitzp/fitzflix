"""Pin the verdict posts of the movie page and the TMDB log page.

The 2 pages share 1 verdict handler since tranche 3. Their intentional
differences stay. The TMDB page never removes a rating or undoes a ✕,
its ✕ never steers the drive, and its messages name the TMDB title.
"""

import pytest

from datetime import datetime

from app import db

from tests.factories import make_movie
from tests.test_reviews import JAWS_2_DETAILS, FakeTMDBDetails
from tests.conftest import csrf_token_from


def _admin_id():
    from app.models import User

    return User.query.filter_by(admin=True).first().id


def _fake_tmdb(app, monkeypatch):
    import app.main.discover as discover

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        discover, "tmdb_get", lambda *a, **k: FakeTMDBDetails(JAWS_2_DETAILS)
    )


def _post_tmdb(admin_client, data):
    page = admin_client.get("/review/tmdb/579").get_data(as_text=True)
    return admin_client.post(
        "/review/tmdb/579",
        data={"csrf_token": csrf_token_from(page), **data},
        follow_redirects=True,
    )


def test_a_tmdb_star_post_names_the_log_a_review(app, admin_client, monkeypatch):
    _fake_tmdb(app, monkeypatch)
    response = _post_tmdb(admin_client, {"quick_rating": "4"})
    assert "Logged review for &#39;Jaws 2 (1978)&#39;" in response.get_data(
        as_text=True
    )


def test_a_tmdb_x_flags_without_steering_the_drive(app, admin_client, monkeypatch):
    from app.elicitation import last_response

    _fake_tmdb(app, monkeypatch)
    response = _post_tmdb(admin_client, {"quick_rating": "0"})
    body = response.get_data(as_text=True)
    assert "Fitzflix will not recommend &#39;Jaws 2 (1978)&#39;." in body
    with app.app_context():
        assert last_response(app.redis, _admin_id()) is None


def test_a_tmdb_x_on_a_flagged_record_keeps_the_flag(app, admin_client, monkeypatch):
    """The TMDB page never undoes a ✕. The movie page toggles it."""

    from app.models import UserMovieStatus

    _fake_tmdb(app, monkeypatch)
    with app.app_context():
        movie = make_movie("Jaws 2", 1978)
        db.session.add(
            UserMovieStatus(
                user_id=_admin_id(), movie_id=movie.id, kind="not_interested"
            )
        )
        db.session.commit()
        movie_id = movie.id

    response = _post_tmdb(admin_client, {"quick_rating": "0"})
    assert "will not recommend" in response.get_data(as_text=True)
    with app.app_context():
        assert UserMovieStatus.query.filter_by(movie_id=movie_id).count() == 1


def _todays_review(stars):
    """Make a Jaws 2 record with no TMDB id and a review of today."""

    from app.models import UserMovieReview
    from app.videos import star_rating_fields

    movie = make_movie("Jaws 2", 1978)
    db.session.add(
        UserMovieReview(
            user_id=_admin_id(),
            movie_id=movie.id,
            liked=stars >= 3,
            date_reviewed=datetime.now(),
            **star_rating_fields(stars),
        )
    )
    db.session.commit()
    return movie.id


@pytest.mark.parametrize("before", [2.0, 4.0])
def test_a_tmdb_star_on_todays_review_corrects_it(
    app, admin_client, monkeypatch, before
):
    """A tap on the same day corrects the row. The same stars never remove it.

    The movie page removes a rating on a tap of the same stars."""

    from app.models import UserMovieReview

    _fake_tmdb(app, monkeypatch)
    with app.app_context():
        movie_id = _todays_review(before)

    _post_tmdb(admin_client, {"quick_rating": "4"})
    with app.app_context():
        (row,) = UserMovieReview.query.filter_by(movie_id=movie_id).all()
        assert float(row.rating) == 4.0
        assert row.liked is True


def test_a_movie_page_x_steers_the_drive(app, admin_client):
    from app.elicitation import last_response

    with app.app_context():
        movie = make_movie("Steer Film", 1999)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": csrf_token_from(page), "quick_rating": "0"},
        follow_redirects=True,
    )
    assert "Fitzflix will not recommend &#39;Steer Film (1999)&#39;" in (
        response.get_data(as_text=True)
    )
    with app.app_context():
        state = last_response(app.redis, _admin_id())
        assert state == {
            "movie_id": movie_id,
            "action": "not_interested",
            "positive": False,
        }

    # A second ✕ removes the flag again.
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": csrf_token_from(page), "quick_rating": "0"},
        follow_redirects=True,
    )
    assert "can be recommended again" in response.get_data(as_text=True)


def test_a_movie_page_star_post_flashes_the_rating(app, admin_client):
    with app.app_context():
        movie = make_movie("Rated Film", 1999)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": csrf_token_from(page), "quick_rating": "4"},
        follow_redirects=True,
    )
    assert "Rated &#39;Rated Film (1999)&#39; 4 out of 5 stars" in response.get_data(
        as_text=True
    )
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": csrf_token_from(page), "quick_rating": "4"},
        follow_redirects=True,
    )
    assert "Removed your rating of &#39;Rated Film (1999)&#39;" in response.get_data(
        as_text=True
    )
