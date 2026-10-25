"""Test the admin forms of the movie, series, and season pages.

The forms are the TMDB lookup and detach, the Criterion details, and
the restore and delete tools of a series and a season. Each form posts
to the URL in ACTIONS. A regular user can never run one.
"""

import re

import pytest

from app import db
from app.models import Movie

from tests.factories import make_movie, make_movie_file

# The URL that each admin form posts to.

ACTIONS = {
    "movie_tmdb": "/movie/{id}",
    "movie_criterion": "/movie/{id}",
}


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


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
