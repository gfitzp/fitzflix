"""Test the admin forms of the movie, series, and season pages.

The forms are the TMDB lookup and detach, the Criterion details, and
the restore and delete tools of a series and a season. Each form posts
to the URL in ACTIONS. A regular user can never run one.
"""

import pytest

from app import db
from app.models import Movie

from tests.factories import make_movie, make_movie_file
from tests.conftest import csrf_token_from

# The URL that each admin form posts to.

ACTIONS = {
    "movie_tmdb": "/movie/{id}/tmdb",
    "movie_criterion": "/movie/{id}/criterion",
}


@pytest.fixture
def no_wait(monkeypatch):
    """Skip the 10 one-second waits for a TMDB refresh result."""

    import app.main.library as library

    monkeypatch.setattr(library.time, "sleep", lambda seconds: None)


def _film(title, year, **kwargs):
    with_files = kwargs.pop("with_files", True)
    movie = make_movie(title, year, **kwargs)
    if with_files:
        make_movie_file(movie, "DVD")
    db.session.commit()
    return movie.id


def test_an_admin_updates_the_criterion_details(app, admin_client):
    with app.app_context():
        movie_id = _film("Spine Film", 1990)

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        ACTIONS["movie_criterion"].format(id=movie_id),
        data={
            "csrf_token": csrf_token_from(page),
            "spine_number": "7",
            "set_title": "Box Set",
            "in_print": "y",
            "quality": "1",
            "criterion_submit": "Update Criterion Info",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/movie/{movie_id}")
    body = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert (
        "Updated Criterion Collection details for &#39;Spine Film (1990)&#39;" in body
    )

    with app.app_context():
        stored = db.session.get(Movie, movie_id)
        assert stored.criterion_spine_number == 7
        assert stored.criterion_set_title == "Box Set"
        assert stored.criterion_in_print is True
        assert stored.criterion_quality_id == 1


def test_a_bad_spine_number_changes_nothing(app, admin_client):
    with app.app_context():
        movie_id = _film("Bad Spine", 1991)

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        ACTIONS["movie_criterion"].format(id=movie_id),
        data={
            "csrf_token": csrf_token_from(page),
            "spine_number": "-3",
            "quality": "1",
            "criterion_submit": "Update Criterion Info",
        },
        follow_redirects=True,
    )
    assert "Enter a positive spine number." in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).criterion_spine_number is None


def test_an_admin_lookup_of_a_known_id_goes_to_that_film(app, admin_client, no_wait):
    """The id belongs to another record. The page goes to that record."""

    with app.app_context():
        movie_id = _film("Jaws Copy", 1975)
        other_id = _film("Jaws", 1975, tmdb_id=578, with_files=False)

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        ACTIONS["movie_tmdb"].format(id=movie_id),
        data={
            "csrf_token": csrf_token_from(page),
            "tmdb_id": "578",
            "lookup_submit": "Refresh TMDB Data",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/movie/{other_id}")
    jobs = [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.refresh_tmdb_info"
    ]
    assert [job.args for job in jobs] == [("Movies", movie_id, 578)]


def test_an_admin_detaches_a_film_with_files(app, admin_client):
    with app.app_context():
        movie_id = _film("Detach Me", 2002, tmdb_id=40222)

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        ACTIONS["movie_tmdb"].format(id=movie_id),
        data={"csrf_token": csrf_token_from(page), "remove_submit": "Remove TMDB ID"},
        follow_redirects=True,
    )
    assert "Removed the TMDB ID from &#39;Detach Me (2002)&#39;" in response.get_data(
        as_text=True
    )
    with app.app_context():
        stored = db.session.get(Movie, movie_id)
        assert stored.tmdb_id is None
        assert stored.tmdb_ignored is True


def _series(app):
    from tests.factories import make_tv_file, make_tv_series

    with app.app_context():
        series = make_tv_series("Admin Routes Show", tmdb_id=110230)
        make_tv_file(series, 1, 1, "DVD")
        db.session.commit()
        return series.id


def _new_routes(movie_id, series_id):
    """Return (url, form data, page that the admin goes back to) of each admin route."""

    return [
        (
            f"/movie/{movie_id}/tmdb",
            {"tmdb_id": "", "lookup_submit": "Refresh TMDB Data"},
            f"/movie/{movie_id}",
        ),
        (
            f"/movie/{movie_id}/criterion",
            {"spine_number": "0", "quality": "1", "criterion_submit": "Update"},
            f"/movie/{movie_id}",
        ),
        (
            f"/tv/{series_id}/tmdb",
            {"tmdb_id": "", "lookup_submit": "Refresh TMDB Data"},
            f"/tv/{series_id}",
        ),
        (
            f"/tv/{series_id}/restore",
            {"password": "wrong", "series_restore_submit": "Bulk restore"},
            f"/tv/{series_id}",
        ),
        (
            f"/tv/{series_id}/delete",
            {},
            f"/tv/{series_id}",
        ),
        (
            f"/tv/{series_id}/1/restore",
            {"password": "wrong", "season_restore_submit": "Bulk restore"},
            f"/tv/{series_id}/1",
        ),
    ]


def test_each_admin_route_goes_back_to_its_page_with_a_message(app, admin_client):
    """Each route answers an admin with a redirect to its page and a flash."""

    with app.app_context():
        movie_id = _film("Routes Film", 1993, tmdb_id=40001)
    series_id = _series(app)
    token = csrf_token_from(admin_client.get("/profile").get_data(as_text=True))
    for url, data, back in _new_routes(movie_id, series_id):
        response = admin_client.post(url, data={"csrf_token": token, **data})
        assert response.status_code == 302, url
        assert response.headers["Location"].endswith(back), url
        with admin_client.session_transaction() as session:
            flashes = session.get("_flashes", [])
        if data:
            assert flashes, url
        with admin_client.session_transaction() as session:
            session.pop("_flashes", None)
    with app.app_context():
        assert db.session.get(Movie, movie_id).criterion_spine_number is None
    assert app.request_queue.count == 0


def test_a_member_cannot_reach_an_admin_route(app, user_client):
    """admin_required sends a regular user to the home page. Nothing runs."""

    from app.models import TVSeries

    with app.app_context():
        movie_id = _film("Members Routes Film", 1994, tmdb_id=40002)
    series_id = _series(app)
    token = csrf_token_from(user_client.get("/profile").get_data(as_text=True))
    for url, data, _ in _new_routes(movie_id, series_id):
        response = user_client.post(
            url, data={"csrf_token": token, "delete_submit": "Delete", **data}
        )
        assert response.status_code == 302, url
        assert response.headers["Location"].endswith("/index"), url
    assert app.sql_queue.count == 0
    assert app.request_queue.count == 0
    with app.app_context():
        assert db.session.get(TVSeries, series_id) is not None
        assert db.session.get(Movie, movie_id).tmdb_id == 40002


def test_the_admin_forms_post_to_their_routes(app, admin_client):
    """The templates name the action of each admin form. None posts to ""."""

    with app.app_context():
        movie_id = _film("Action Film", 1995, tmdb_id=40003)
    series_id = _series(app)
    movie_page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert movie_page.count(f'action="/movie/{movie_id}/tmdb"') == 2
    assert f'action="/movie/{movie_id}/criterion"' in movie_page
    tv_page = admin_client.get(f"/tv/{series_id}").get_data(as_text=True)
    assert tv_page.count(f'action="/tv/{series_id}/tmdb"') == 2
    assert f'action="/tv/{series_id}/restore"' in tv_page
    assert f'action="/tv/{series_id}/delete"' in tv_page
    assert 'action=""' not in tv_page
    season_page = admin_client.get(f"/tv/{series_id}/1").get_data(as_text=True)
    assert f'action="/tv/{series_id}/1/restore"' in season_page
    assert 'action=""' not in season_page


def test_the_series_and_season_pages_take_no_post(app, admin_client):
    series_id = _series(app)
    assert admin_client.post(f"/tv/{series_id}").status_code == 405
    assert admin_client.post(f"/tv/{series_id}/1").status_code == 405
    assert admin_client.get(f"/tv/{series_id}/delete").status_code == 405
