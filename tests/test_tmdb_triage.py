"""The TMDB triage page (#226): the maintenance surface that answers
"which records are unmatched, and what do I want to do about each one?"
It lists every movie and series at a NULL tmdb_id without the ignored
flag, and each row either gets flagged as unmatchable (the Remove
button's clear path) or matched to an id entered by hand."""

import re

from app import db
from app.models import Movie, TVSeries

from tests.factories import make_movie, make_movie_file, make_tv_series


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


class _InstantTime:
    """Stands in for the admin module's time so the match action's
    wait-for-the-refresh loop doesn't stall the tests."""

    @staticmethod
    def sleep(seconds):
        pass


def test_page_lists_only_unmatched_unflagged_records(app, admin_client):
    with app.app_context():
        unmatched = make_movie("Mystery Reel", 1980)
        make_movie_file(unmatched, "DVD")
        make_movie("Matched Film", 1981, tmdb_id=500)
        make_movie("Home Movie Flagged", 1982, tmdb_ignored=True)
        make_tv_series("Local Sports")
        make_tv_series("Known Show", tmdb_id=600)
        db.session.commit()

    page = admin_client.get("/maintenance/tmdb").get_data(as_text=True)
    assert "Mystery Reel" in page
    assert "Local Sports" in page
    assert "Matched Film" not in page
    assert "Home Movie Flagged" not in page
    assert "Known Show" not in page

    # Each row carries the decision's ingredients — file count, date,
    # the search hand-off — and both actions

    assert page.count("Search TMDB") == 2
    assert page.count('name="flag_submit"') == 2
    assert page.count('name="lookup_submit"') == 2
    assert "2 records with no TMDB id" in page

    # The maintenance page's card goes warning-coloured on a non-zero
    # count

    mpage = admin_client.get("/maintenance").get_data(as_text=True)
    assert "Triage TMDB matches" in mpage
    assert re.search(r'btn-warning" href="[^"]*/maintenance/tmdb"', mpage)


def test_empty_state_and_neutral_card(app, admin_client):
    with app.app_context():
        make_movie("Matched Film", 1981, tmdb_id=500)
        db.session.commit()

    page = admin_client.get("/maintenance/tmdb").get_data(as_text=True)
    assert "nothing to triage" in page

    mpage = admin_client.get("/maintenance").get_data(as_text=True)
    assert re.search(r'btn-secondary" href="[^"]*/maintenance/tmdb"', mpage)


def test_flag_marks_movie_and_series_unmatchable(app, admin_client):
    with app.app_context():
        movie = make_movie("Basement Cleanup", 1999)
        series = make_tv_series("Corgi Races")
        db.session.commit()
        movie_id, series_id = movie.id, series.id

    page = admin_client.get("/maintenance/tmdb").get_data(as_text=True)
    token = csrf_token_from(page)

    response = admin_client.post(
        "/maintenance/tmdb",
        data={
            "csrf_token": token,
            "movie_id": movie_id,
            "flag_submit": "Flag as unmatchable",
        },
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "Flagged &#39;Basement Cleanup (1999)&#39; as unmatchable" in body
    assert "Basement Cleanup" not in body.replace(
        "Flagged &#39;Basement Cleanup (1999)&#39; as unmatchable", ""
    )

    response = admin_client.post(
        "/maintenance/tmdb",
        data={
            "csrf_token": token,
            "series_id": series_id,
            "flag_submit": "Flag as unmatchable",
        },
        follow_redirects=True,
    )
    assert "Flagged &#39;Corgi Races&#39; as unmatchable" in response.get_data(
        as_text=True
    )

    with app.app_context():
        assert db.session.get(Movie, movie_id).tmdb_ignored is True
        assert db.session.get(TVSeries, series_id).tmdb_ignored is True


def test_flag_refuses_a_record_that_left_the_list(app, admin_client):
    """The guard against a stale page: a record matched (or flagged)
    since the page rendered must never be cleared by a late flag."""

    with app.app_context():
        matched = make_movie("Raced Ahead", 1990, tmdb_id=777, tmdb_title="Raced")
        # A still-unmatched sibling keeps a triage form (and its csrf
        # token) on the page, the way a stale tab would have one
        make_movie("Still Waiting", 1991)
        db.session.commit()
        matched_id = matched.id

    page = admin_client.get("/maintenance/tmdb").get_data(as_text=True)
    response = admin_client.post(
        "/maintenance/tmdb",
        data={
            "csrf_token": csrf_token_from(page),
            "movie_id": matched_id,
            "flag_submit": "Flag as unmatchable",
        },
        follow_redirects=True,
    )
    assert "is not waiting for TMDB triage" in response.get_data(as_text=True)
    with app.app_context():
        record = db.session.get(Movie, matched_id)
        assert record.tmdb_ignored is False
        assert record.tmdb_id == 777
        assert record.tmdb_title == "Raced"


def test_match_enqueues_the_refresh(app, admin_client, monkeypatch):
    import app.main.admin as admin

    monkeypatch.setattr(admin, "time", _InstantTime)

    with app.app_context():
        movie = make_movie("G.I. Joe- The Ernie Pyle Story", 1988)
        series = make_tv_series("Shogun (1980)")
        db.session.commit()
        movie_id, series_id = movie.id, series.id

    page = admin_client.get("/maintenance/tmdb").get_data(as_text=True)
    token = csrf_token_from(page)

    admin_client.post(
        "/maintenance/tmdb",
        data={
            "csrf_token": token,
            "movie_id": movie_id,
            "tmdb_id": "265216",
            "lookup_submit": "Match",
        },
    )
    admin_client.post(
        "/maintenance/tmdb",
        data={
            "csrf_token": token,
            "series_id": series_id,
            "tmdb_id": "13911",
            "lookup_submit": "Match",
        },
    )

    jobs = [
        job.args
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.refresh_tmdb_info"
    ]
    assert ("Movies", movie_id, 265216) in jobs
    assert ("TV Shows", series_id, 13911) in jobs


def test_match_without_an_id_is_refused(app, admin_client):
    with app.app_context():
        movie = make_movie("Needs An Id", 1970)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get("/maintenance/tmdb").get_data(as_text=True)
    response = admin_client.post(
        "/maintenance/tmdb",
        data={
            "csrf_token": csrf_token_from(page),
            "movie_id": movie_id,
            "lookup_submit": "Match",
        },
        follow_redirects=True,
    )
    assert "Enter a TMDB ID" in response.get_data(as_text=True)
    assert not [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.refresh_tmdb_info"
    ]
