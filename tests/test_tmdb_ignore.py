"""Detaching a record from TMDB (#207).

A NULL tmdb_id means "not matched yet" and every refresh path answers it
with a title search; tmdb_ignored means "TMDB has nothing to match" and
every refresh path has to leave the record alone. These tests pin both
halves: the clear methods empty the record, and the refresh pair, the
nightly sweep and the bulk refresh all decline it afterwards.
"""

import re

from datetime import datetime

from app import db
from app.models import (
    Movie,
    MovieCast,
    MovieCrew,
    TMDBCredit,
    TMDBGenre,
    TVCast,
    TVSeries,
)

from tests.factories import make_movie, make_movie_file, make_tv_series


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def enriched_movie():
    """A film carrying the full spread of TMDB enrichment."""

    movie = make_movie(
        "Pompeii The Last Day",
        2003,
        tmdb_id=40222,
        imdb_id="tt0369838",
        tmdb_title="Pompeii: The Last Day",
        tmdb_overview="A dramatization.",
        tmdb_poster_path="/poster.jpg",
        tmdb_runtime=49,
        tmdb_status="Released",
        tmdb_vote_average=6.5,
        tmdb_data_as_of=datetime(2020, 9, 25),
    )
    credit = TMDBCredit(id=99001, name="Tim Pigott-Smith")
    genre = TMDBGenre(id=99, name="Docudrama")
    db.session.add_all([credit, genre])
    db.session.flush()
    db.session.add(
        MovieCast(movie_id=movie.id, credit_id=credit.id, character="Narrator")
    )
    db.session.add(
        MovieCrew(
            movie_id=movie.id,
            credit_id=credit.id,
            department="Directing",
            job="Director",
        )
    )
    movie.genres.append(genre)
    db.session.flush()
    return movie


def enriched_series():
    """A series carrying TMDB enrichment, cast and stored episodes."""

    series = make_tv_series(
        "Rifftrax",
        tmdb_id=110230,
        imdb_id="tt1234567",
        tvdb_id=4242,
        tmdb_name="Rifftrax",
        tmdb_overview="Riffing.",
        tmdb_in_production=True,
        tmdb_status="Returning Series",
        tmdb_data_as_of=datetime(2020, 9, 25),
    )
    credit = TMDBCredit(id=99002, name="Michael J. Nelson")
    db.session.add(credit)
    db.session.flush()
    db.session.add(TVCast(tv_id=series.id, credit_id=credit.id, character="Himself"))
    db.session.flush()
    return series


def test_movie_clear_empties_every_tmdb_field_and_association(app):
    with app.app_context():
        movie = enriched_movie()
        movie_id = movie.id
        movie.tmdb_movie_clear()
        db.session.commit()
        db.session.expire_all()

        stored = db.session.get(Movie, movie_id)
        assert stored.tmdb_id is None
        assert stored.imdb_id is None
        assert stored.tmdb_title is None
        assert stored.tmdb_overview is None
        assert stored.tmdb_poster_path is None
        assert stored.tmdb_runtime is None
        assert stored.tmdb_status is None
        assert stored.tmdb_vote_average is None
        assert stored.tmdb_data_as_of is None
        assert stored.tmdb_ignored is True

        assert stored.genres.count() == 0
        assert MovieCast.query.filter_by(movie_id=movie_id).count() == 0
        assert MovieCrew.query.filter_by(movie_id=movie_id).count() == 0

        # The film's own library identity is not TMDB's to take away

        assert stored.title == "Pompeii The Last Day"
        assert stored.year == 2003


def test_tv_clear_empties_fields_cast_and_episodes(app):
    with app.app_context():
        series = enriched_series()
        series_id = series.id
        series.tmdb_tv_clear()
        db.session.commit()
        db.session.expire_all()

        stored = db.session.get(TVSeries, series_id)
        assert stored.tmdb_id is None
        assert stored.imdb_id is None
        assert stored.tvdb_id is None
        assert stored.tmdb_name is None
        assert stored.tmdb_overview is None
        assert stored.tmdb_in_production is None
        assert stored.tmdb_status is None
        assert stored.tmdb_data_as_of is None
        assert stored.tmdb_ignored is True

        assert TVCast.query.filter_by(tv_id=series_id).count() == 0
        assert stored.title == "Rifftrax"


def test_refresh_declines_an_ignored_movie(app):
    from app.videos import refresh_tmdb_info

    with app.app_context():
        movie = make_movie("1982 Glenn", 1982, tmdb_ignored=True)
        db.session.commit()
        movie_id = movie.id

    assert refresh_tmdb_info("Movies", movie_id) is False
    assert app.sql_queue.count == 0


def test_refresh_declines_an_ignored_series(app):
    from app.videos import refresh_tmdb_info

    with app.app_context():
        series = make_tv_series("Baltimore Orioles", tmdb_ignored=True)
        db.session.commit()
        series_id = series.id

    assert refresh_tmdb_info("TV Shows", series_id) is False
    assert app.sql_queue.count == 0


def test_apply_discards_a_payload_for_a_record_ignored_since_the_fetch(app):
    """A refresh already in flight when the user detaches the record must
    not write the payload it fetched."""

    import json
    import zlib

    from app.videos import apply_tmdb_refresh

    with app.app_context():
        series = make_tv_series("Comedy Central Stand Up Specials")
        series_id = series.id
        db.session.commit()

    payload = zlib.compress(
        json.dumps({"id": 53361, "name": "Some Other Show"}).encode("utf-8")
    )

    with app.app_context():
        db.session.get(TVSeries, series_id).tmdb_ignored = True
        db.session.commit()

    assert (
        apply_tmdb_refresh(
            library="TV Shows", id=series_id, tmdb_id=53361, tmdb_payload=payload
        )
        is False
    )

    with app.app_context():
        stored = db.session.get(TVSeries, series_id)
        assert stored.tmdb_id is None
        assert stored.tmdb_name is None


def test_nightly_sweep_skips_ignored_series(app):
    from app.tmdb_refresh import refresh_in_production_tv

    with app.app_context():
        make_tv_series("Star Trek", tmdb_id=253, tmdb_in_production=True)

        # An ignored series that still somehow carries an id is excluded
        # on the flag alone, not just on the NULL

        make_tv_series(
            "Private Snafu", tmdb_id=999999, tmdb_in_production=True, tmdb_ignored=True
        )
        db.session.commit()

    assert refresh_in_production_tv() == 1
    assert [job.args[2] for job in app.request_queue.jobs] == [253]


def test_bulk_refresh_leaves_ignored_records_alone(app, admin_client):
    with app.app_context():
        make_movie("Jaws", 1975, tmdb_id=578)
        make_movie("1984 Wisconsin", 1984, tmdb_ignored=True)
        make_tv_series("Star Trek", tmdb_id=253)
        make_tv_series("Washington Capitals", tmdb_ignored=True)
        db.session.commit()

    page = admin_client.get("/maintenance").get_data(as_text=True)
    admin_client.post(
        "/maintenance",
        data={
            "csrf_token": csrf_token_from(page),
            "tmdb_refresh": "Refresh TMDB Info",
        },
        follow_redirects=True,
    )

    queued = [
        job.args
        for job in app.request_queue.jobs
        if job.func_name == "app.videos.refresh_tmdb_info"
    ]
    assert sorted(job[0] for job in queued) == ["Movies", "TV Shows"]
    assert {job[2] for job in queued} == {578, 253}


def test_blank_lookup_on_a_matched_series_is_refused(app, admin_client):
    """The #207 report: clearing the id field ran a title search and
    re-pointed the series at whatever came back first."""

    with app.app_context():
        series = make_tv_series("Rifftrax", tmdb_id=110230)
        db.session.commit()
        series_id = series.id

    page = admin_client.get(f"/tv/{series_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/tv/{series_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "tmdb_id": "",
            "lookup_submit": "Refresh TMDB Data",
        },
        follow_redirects=True,
    )

    assert "Remove TMDB ID" in response.get_data(as_text=True)
    assert app.sql_queue.count == 0

    with app.app_context():
        assert db.session.get(TVSeries, series_id).tmdb_id == 110230


def test_remove_button_detaches_the_series(app, admin_client):
    with app.app_context():
        series = enriched_series()
        db.session.commit()
        series_id = series.id

    page = admin_client.get(f"/tv/{series_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/tv/{series_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "remove_submit": "Remove TMDB ID",
        },
        follow_redirects=True,
    )

    assert "Removed the TMDB ID" in response.get_data(as_text=True)

    with app.app_context():
        stored = db.session.get(TVSeries, series_id)
        assert stored.tmdb_id is None
        assert stored.tmdb_ignored is True


def test_entering_an_id_by_hand_reattaches_an_ignored_series(app, admin_client):
    with app.app_context():
        series = make_tv_series("Rifftrax", tmdb_ignored=True)
        db.session.commit()
        series_id = series.id

    page = admin_client.get(f"/tv/{series_id}").get_data(as_text=True)
    admin_client.post(
        f"/tv/{series_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "tmdb_id": "110230",
            "lookup_submit": "Refresh TMDB Data",
        },
        follow_redirects=True,
    )

    with app.app_context():
        assert db.session.get(TVSeries, series_id).tmdb_ignored is False

    jobs = [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.refresh_tmdb_info"
    ]
    assert jobs and jobs[0].args == ("TV Shows", series_id, 110230)


def test_tv_clear_invalidates_the_people_ranking(app):
    """Detaching a series deletes its TVCast/TVCrew rows, and the /people
    rankings aggregate TV credits too — so the cached rankings have to go
    the way the movie clear already sends them."""

    from app.models import PEOPLE_RANKING_KEY

    with app.app_context():
        series = enriched_series()
        db.session.commit()
        for role in ("cast", "crew", "all"):
            app.redis.set(PEOPLE_RANKING_KEY.format(role=role), "[]")
        series.tmdb_tv_clear()
        db.session.commit()

    for role in ("cast", "crew", "all"):
        assert app.redis.get(PEOPLE_RANKING_KEY.format(role=role)) is None


def test_blank_lookup_on_a_detached_series_stays_detached(app, admin_client):
    """A detached record has tmdb_id NULL, so the matched-record guard
    alone let a blank refresh clear tmdb_ignored and run the very title
    search detaching was meant to prevent. The flag only clears for an
    id entered by hand."""

    with app.app_context():
        series = make_tv_series("Home Movies Reel", tmdb_ignored=True)
        db.session.commit()
        series_id = series.id

    page = admin_client.get(f"/tv/{series_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/tv/{series_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "tmdb_id": "",
            "lookup_submit": "Refresh TMDB Data",
        },
        follow_redirects=True,
    )

    assert "Enter a TMDB ID to refresh this series" in response.get_data(as_text=True)
    assert app.sql_queue.count == 0

    with app.app_context():
        assert db.session.get(TVSeries, series_id).tmdb_ignored is True


def test_blank_lookup_on_a_detached_movie_stays_detached(app, admin_client):
    """The movie route's copy of the same guard."""

    with app.app_context():
        movie = make_movie("Family Reunion Tape", 1994, tmdb_ignored=True)
        make_movie_file(movie, "DVD")
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "tmdb_id": "",
            "lookup_submit": "Refresh TMDB Data",
        },
        follow_redirects=True,
    )

    assert "Enter a TMDB ID to refresh this movie" in response.get_data(as_text=True)
    assert app.sql_queue.count == 0

    with app.app_context():
        assert db.session.get(Movie, movie_id).tmdb_ignored is True


def test_movie_data_section_is_admin_only(app, admin_client, user_client):
    """The Movie Data forms are admin tools (#186 follow-up): the section
    doesn't render for regular users, and hand-crafted posts bounce
    server-side without touching the record."""

    with app.app_context():
        movie = make_movie("Members Film", 1988, tmdb_id=41414)
        make_movie_file(movie, "DVD")
        db.session.commit()
        movie_id = movie.id

    page = user_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Movie Data" not in page
    assert "lookup_submit" not in page
    assert "remove_submit" not in page
    assert "criterion_submit" not in page
    assert f"/movie/{movie_id}/poster" not in page

    admin_page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Movie Data" in admin_page
    assert "lookup_submit" in admin_page

    for tampered in (
        {"tmdb_id": "999", "lookup_submit": "Refresh TMDB Data"},
        {"remove_submit": "Remove TMDB ID"},
        {
            "spine_number": "7",
            "quality": "1",
            "criterion_submit": "Update Criterion Info",
        },
    ):
        response = user_client.post(
            f"/movie/{movie_id}",
            data={"csrf_token": csrf_token_from(page), **tampered},
            follow_redirects=True,
        )
        assert "admin" in response.get_data(as_text=True)
    assert app.sql_queue.count == 0

    with app.app_context():
        stored = db.session.get(Movie, movie_id)
        assert stored.tmdb_id == 41414
        assert stored.criterion_spine_number is None


def test_fileless_record_locks_its_tmdb_id(app, admin_client, monkeypatch):
    """A record with no local files mirrors its TMDB entry: admins see
    the id read-only with a refresh-only button — no re-point, no
    detach — and a smuggled id in the post is ignored, so the diary rows
    stay on the film they were logged against."""

    import app.main.library as library

    monkeypatch.setattr(library.time, "sleep", lambda seconds: None)

    with app.app_context():
        movie = make_movie("Logged Only Film", 2001, tmdb_id=52520)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "TMDB ID: 52520" in page
    assert 'name="tmdb_id"' not in page
    assert "Remove TMDB ID" not in page
    assert f"/movie/{movie_id}/poster" not in page
    assert "Refresh TMDB Data" in page
    assert "criterion_submit" in page  # the Criterion form stays

    response = admin_client.post(
        f"/movie/{movie_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "tmdb_id": "999",
            "lookup_submit": "Refresh TMDB Data",
        },
    )
    assert response.status_code == 302

    jobs = [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.refresh_tmdb_info"
    ]
    assert jobs and jobs[0].args == ("Movies", movie_id, 52520)

    with app.app_context():
        assert db.session.get(Movie, movie_id).tmdb_id == 52520


def test_fileless_record_refuses_detach(app, admin_client):
    """Even an admin can't detach a file-less record: it has nothing but
    its TMDB mirror behind it, so tmdb_movie_clear would leave a husk."""

    with app.app_context():
        movie = make_movie("Logged Only Film", 2001, tmdb_id=52520)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "remove_submit": "Remove TMDB ID",
        },
        follow_redirects=True,
    )
    assert "mirrors TMDB" in response.get_data(as_text=True)

    with app.app_context():
        stored = db.session.get(Movie, movie_id)
        assert stored.tmdb_id == 52520
        assert stored.tmdb_ignored is not True


def test_tv_management_forms_are_admin_only(app, admin_client, user_client):
    """The TV page's management forms — transcode, restore, delete, and
    the TMDB pair — are admin tools like the movie page's Movie Data
    section: hidden from regular users, with every branch bouncing
    hand-crafted posts server-side before touching anything."""

    from tests.factories import make_tv_file

    with app.app_context():
        series = make_tv_series("Members Show", tmdb_id=110230)
        make_tv_file(series, 1, 1, "DVD")
        db.session.commit()
        series_id = series.id

    page = user_client.get(f"/tv/{series_id}").get_data(as_text=True)
    for control in (
        "lookup_submit",
        "remove_submit",
        "transcode_all",
        "series_restore_submit",
        "delete_submit",
    ):
        assert control not in page, control
    assert "Seasons" in page  # the consumer-facing controls stay

    admin_page = admin_client.get(f"/tv/{series_id}").get_data(as_text=True)
    for control in (
        "lookup_submit",
        "remove_submit",
        "transcode_all",
        "series_restore_submit",
        "delete_submit",
    ):
        assert control in admin_page, control

    # The gated page carries no forms for a regular user, so borrow the
    # session-wide csrf token from a page that still has one

    token = csrf_token_from(user_client.get("/profile").get_data(as_text=True))
    for tampered in (
        {"tmdb_id": "999", "lookup_submit": "Refresh TMDB Data"},
        {"remove_submit": "Remove TMDB ID"},
        {"transcode_all": "Transcode All"},
        {"password": "hunter2", "series_restore_submit": "Bulk restore"},
        {"delete_submit": "Delete Series"},
    ):
        response = user_client.post(
            f"/tv/{series_id}",
            data={"csrf_token": token, **tampered},
            follow_redirects=True,
        )
        assert "admin" in response.get_data(as_text=True), tampered

    assert app.sql_queue.count == 0
    assert app.transcode_queue.count == 0
    assert app.request_queue.count == 0

    with app.app_context():
        stored = db.session.get(TVSeries, series_id)
        assert stored is not None  # the delete never ran
        assert stored.tmdb_id == 110230
