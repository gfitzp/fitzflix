"""Test the title that an admin selects for a film.

A refresh gives a film the main title of TMDB. An admin can select a
different title from the titles that TMDB lists for the film. The
refresh keeps the selected title while TMDB lists it. The year follows
TMDB in each case."""

import json
import logging
import os
import zlib

import pytest

from app import db
from tests.conftest import page_csrf_token
from tests.factories import make_movie, make_movie_file

MAIN = "Once Upon a Time in... Hollywood"
STREAMING = "Once Upon a Time in Hollywood"
TMDB_ID = 466272


def payload(title=MAIN, alternatives=(STREAMING,), **extra):
    """Return a details payload with its block of alternative titles."""

    details = {
        "id": TMDB_ID,
        "title": title,
        "original_title": title,
        "release_date": "2019-07-24",
        **extra,
    }
    if alternatives is not None:
        details["alternative_titles"] = {
            "titles": [
                {"iso_3166_1": "US", "title": name, "type": "streaming title"}
                for name in alternatives
            ]
        }
    return details


def packed(details):
    return zlib.compress(json.dumps(details).encode("utf-8"))


def film(title=STREAMING, chosen=STREAMING, year=2019):
    movie = make_movie(title, year, tmdb_id=TMDB_ID, chosen_title=chosen)
    movie.tmdb_title = title
    db.session.commit()
    return movie


# The rule of the refresh


def test_the_titles_of_a_payload_are_the_main_the_original_and_the_alternatives(app):
    from app.metadata.tmdb_apply import tmdb_title_choices, tmdb_titles

    details = payload(
        original_title="Il était une fois", alternatives=(STREAMING, MAIN.upper())
    )
    with app.app_context():
        assert tmdb_titles(details) == [MAIN, "Il était une fois", STREAMING]
        assert tmdb_title_choices(details) == [
            {"title": MAIN, "sources": ["TMDB title", "US: streaming title"]},
            {"title": "Il était une fois", "sources": ["Original title"]},
            {"title": STREAMING, "sources": ["US: streaming title"]},
        ]
        # A payload with no block of alternative titles has no complete list.
        assert tmdb_titles(payload(alternatives=None)) is None
        assert tmdb_titles(payload(alternatives=())) == [MAIN]


def test_a_selected_title_stays_while_tmdb_lists_it(app):
    with app.app_context():
        movie = film()
        movie.tmdb_movie_apply(payload())
        db.session.commit()
        assert (movie.title, movie.year) == (STREAMING, 2019)
        assert movie.tmdb_title == STREAMING
        assert movie.chosen_title == STREAMING

        # An edit of the main title on TMDB does not rename the film.
        movie.tmdb_movie_apply(payload(title="Once Upon a Time... in Hollywood"))
        db.session.commit()
        assert (movie.title, movie.tmdb_title) == (STREAMING, STREAMING)
        assert movie.chosen_title == STREAMING


def test_the_year_of_a_film_with_a_selected_title_follows_tmdb(app):
    with app.app_context():
        movie = film()
        movie.tmdb_movie_apply(payload(release_date="2020-01-01"))
        db.session.commit()
        assert (movie.title, movie.year) == (STREAMING, 2020)


@pytest.mark.parametrize(
    "listed",
    [
        "once upon a time in hollywood",
        "Once  Upon a Time in Hollywood ",
        "Once Upon a Time in Hollywóod",
    ],
)
def test_letter_case_spaces_and_accents_do_not_end_a_selection(app, listed):
    with app.app_context():
        movie = film()
        movie.tmdb_movie_apply(payload(alternatives=(listed,)))
        db.session.commit()
        assert movie.title == STREAMING
        assert movie.chosen_title == STREAMING


def test_an_ellipsis_character_and_3_periods_are_the_same_title(app):
    with app.app_context():
        movie = film(title="Once Upon a Time in… Hollywood", chosen=None)
        movie.chosen_title = "Once Upon a Time in… Hollywood"
        movie.tmdb_movie_apply(payload(title="Other Main", alternatives=(MAIN,)))
        db.session.commit()
        assert movie.title == "Once Upon a Time in… Hollywood"
        assert movie.chosen_title == "Once Upon a Time in… Hollywood"


def test_a_title_that_tmdb_no_longer_lists_gives_way_to_the_main_title(app, caplog):
    with app.app_context():
        movie = film()
        with caplog.at_level(logging.WARNING, logger="app"):
            movie.tmdb_movie_apply(payload(alternatives=("Another Name",)))
        db.session.commit()
        assert movie.title == MAIN
        assert movie.tmdb_title == MAIN
        assert movie.chosen_title is None
    assert [
        record.getMessage()
        for record in caplog.records
        if "no longer lists" in record.getMessage()
    ] == [
        f"<Movie '{STREAMING} (2019)'> TMDB no longer lists the selected title "
        f"'{STREAMING}'. The film gets the TMDB title again"
    ]


def test_a_payload_with_no_list_of_alternative_titles_keeps_the_selection(app):
    """A payload from before the change waits in a queue with no such block."""

    with app.app_context():
        movie = film()
        movie.tmdb_movie_apply(payload(alternatives=None))
        db.session.commit()
        assert (movie.title, movie.tmdb_title) == (STREAMING, STREAMING)
        assert movie.chosen_title == STREAMING


def test_an_empty_list_of_alternative_titles_keeps_the_selection(app):
    """TMDB sent whole payloads with an empty list where the film has entries."""

    with app.app_context():
        movie = film()
        movie.tmdb_movie_apply(payload(alternatives=()))
        db.session.commit()
        assert (movie.title, movie.tmdb_title) == (STREAMING, STREAMING)
        assert movie.chosen_title == STREAMING


def test_an_entry_that_is_not_an_object_does_not_stop_the_apply(app):
    from app.metadata.tmdb_apply import tmdb_title_choices

    details = payload()
    details["alternative_titles"]["titles"] += ["a bare string", None, {"title": 5}]
    with app.app_context():
        assert [choice["title"] for choice in tmdb_title_choices(details)] == [
            MAIN,
            STREAMING,
        ]
        movie = film()
        movie.tmdb_movie_apply(details)
        db.session.commit()
        assert movie.title == STREAMING


def test_2_scripts_are_2_choices_and_white_space_is_1_space(app):
    from app.metadata.tmdb_apply import tmdb_titles

    details = payload(
        title="Stalker", alternatives=("Сталкер", "Stalker\u00a0 II", "STALKER")
    )
    with app.app_context():
        assert tmdb_titles(details) == ["Stalker", "Сталкер", "Stalker II"]


def test_a_film_with_no_selection_follows_the_main_title(app):
    """An old title that TMDB lists as an alternative does not stay by itself."""

    with app.app_context():
        movie = film(chosen=None)
        movie.tmdb_movie_apply(payload())
        db.session.commit()
        assert (movie.title, movie.tmdb_title) == (MAIN, MAIN)
        assert movie.chosen_title is None


def test_the_canonical_name_of_a_selection_is_the_selected_title(app):
    """The title locks of the apply use this name."""

    from app.metadata.tmdb_apply import canonical_movie_name

    with app.app_context():
        assert canonical_movie_name(payload(), 1999) == (MAIN, 2019)
        assert canonical_movie_name(payload(), 1999, STREAMING) == (STREAMING, 2019)
        other = payload(alternatives=("Another Name",))
        assert canonical_movie_name(other, 1999, STREAMING) == (MAIN, 2019)
        assert canonical_movie_name(payload(alternatives=()), 1999, STREAMING) == (
            STREAMING,
            2019,
        )
        assert canonical_movie_name(payload(alternatives=None), 1999, STREAMING) == (
            STREAMING,
            2019,
        )


def test_the_apply_locks_the_selected_name(app, held_lock):
    """An import under the selected name cannot run during the rename."""

    from app.metadata.tmdb_refresh import _movie_base_lock, apply_tmdb_refresh

    with app.app_context():
        movie = film(title=MAIN)
        movie_id = movie.id
        held_lock(_movie_base_lock(STREAMING, 2019))
        assert (
            apply_tmdb_refresh(
                "Movies", movie_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is False
        )
        db.session.expire_all()
        assert db.session.get(type(movie), movie_id).title == MAIN


def test_a_selection_renames_the_folder_and_the_files(app):
    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import File, Movie

    with app.app_context():
        movie = film(title=MAIN, chosen=STREAMING)
        file = make_movie_file(movie, "Bluray-1080p")
        file.untouched_basename = file.basename
        db.session.commit()
        movie_id, file_id = movie.id, file.id
        old_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(old_path), exist_ok=True)
        with open(old_path, "wb") as handle:
            handle.write(b"payload")

        assert (
            apply_tmdb_refresh(
                "Movies", movie_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is True
        )
        db.session.expire_all()
        row = db.session.get(File, file_id)
        name = f"{STREAMING} (2019) - [Bluray-1080p].mkv"
        assert db.session.get(Movie, movie_id).title == STREAMING
        assert row.untouched_basename == name
        assert row.file_path == f"Movies/{STREAMING} (2019)/{name}"
        assert not os.path.exists(old_path)
        assert os.path.exists(os.path.join(app.config["LIBRARY_DIR"], row.file_path))

        # A later edit of the main title on TMDB moves nothing.
        assert (
            apply_tmdb_refresh(
                "Movies",
                movie_id,
                tmdb_id=TMDB_ID,
                tmdb_payload=packed(payload(title="A New Main Title")),
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(File, file_id).file_path == row.file_path


def on_disk(app, file):
    """Give a file row its untouched name and a file in the library."""

    file.untouched_basename = file.basename
    db.session.commit()
    path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"payload")
    return path


def test_a_merge_gives_the_survivor_the_selection(app):
    """The record that goes holds the name until its delete. A second refresh ends the rename."""

    from datetime import datetime

    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import File, Movie

    with app.app_context():
        survivor = make_movie(MAIN, 2019, tmdb_id=TMDB_ID)
        survivor.tmdb_title = MAIN
        survivor.date_created = datetime(2020, 1, 1)
        newer = make_movie(STREAMING, 2019, tmdb_id=TMDB_ID, chosen_title=STREAMING)
        newer.tmdb_title = STREAMING
        newer.date_created = datetime(2024, 1, 1)
        db.session.commit()
        file = make_movie_file(newer, "Bluray-1080p")
        path = on_disk(app, file)
        survivor_id, newer_id, file_id = survivor.id, newer.id, file.id

        assert (
            apply_tmdb_refresh(
                "Movies", newer_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, newer_id) is None
        kept = db.session.get(Movie, survivor_id)
        assert kept.chosen_title == STREAMING
        assert db.session.get(File, file_id).movie_id == survivor_id

        def queued():
            return [
                job.args
                for job in app.request_queue.jobs
                if job.func_name == "app.metadata.tmdb_refresh.refresh_tmdb_info"
            ]

        assert queued() == [("Movies", survivor_id, TMDB_ID)]

        assert (
            apply_tmdb_refresh(
                "Movies", survivor_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is True
        )
        db.session.expire_all()
        kept = db.session.get(Movie, survivor_id)
        assert (kept.title, kept.tmdb_title, kept.chosen_title) == (STREAMING,) * 3
        row = db.session.get(File, file_id)
        assert row.file_path.startswith(f"Movies/{STREAMING} (2019)/")
        assert os.path.exists(os.path.join(app.config["LIBRARY_DIR"], row.file_path))
        assert not os.path.exists(path) or path.endswith(row.file_path)
        # The second refresh is not a merge. It queues nothing more.
        assert len(queued()) == 1


def test_a_rematch_does_not_carry_the_selection_of_an_other_film(app):
    from datetime import datetime

    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import Movie

    with app.app_context():
        target = make_movie(MAIN, 2019, tmdb_id=TMDB_ID)
        target.tmdb_title = MAIN
        target.date_created = datetime(2020, 1, 1)
        other = make_movie("Gamma Alt", 1990, tmdb_id=77, chosen_title="Gamma Alt")
        other.tmdb_title = "Gamma Alt"
        other.date_created = datetime(2024, 1, 1)
        db.session.commit()
        on_disk(app, make_movie_file(other, "Bluray-1080p"))
        target_id, other_id = target.id, other.id

        assert (
            apply_tmdb_refresh(
                "Movies",
                other_id,
                tmdb_id=TMDB_ID,
                tmdb_payload=packed(payload()),
                rematch=True,
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, other_id) is None
        kept = db.session.get(Movie, target_id)
        assert (kept.title, kept.chosen_title) == (MAIN, None)


def test_a_file_with_the_selected_title_goes_to_the_film_with_the_selection(
    app, monkeypatch
):
    """A restored rip has the selected title in its name, and no id tag."""

    from app.media.importing import filenames

    asked = []

    def fake_json(path, params=None, timeout=10):
        asked.append(path)
        return {
            "results": [{"id": 77, "title": STREAMING, "release_date": "2019-01-01"}]
        }

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(filenames, "tmdb_json", fake_json)
    with app.app_context():
        film()
        name = f"{STREAMING} (2019) - [Bluray-1080p].mkv"
        details = filenames.evaluate_filename(name, log=False)
        assert (details["title"], details["tmdb_id"]) == (STREAMING, TMDB_ID)
        assert asked == []

        # A film with no selection still goes through the TMDB search.
        make_movie("Plain Title", 2001, tmdb_id=5)
        db.session.commit()
        filenames.evaluate_filename("Plain Title (2001) - [DVD].mkv", log=False)
        assert asked == ["/search/movie"]


def test_a_rating_import_finds_the_film_with_a_selection_by_its_tmdb_id(
    app, monkeypatch
):
    """The export names the film with its TMDB title. No second record appears."""

    from app.metadata import tmdb_apply
    from app.models import Movie, User
    from app.viewing import diary

    def fake_json(path, params=None, timeout=10):
        if path == "/search/movie":
            return {"results": [{"id": TMDB_ID, "title": MAIN}]}
        return payload()

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(diary, "tmdb_json", fake_json)
    monkeypatch.setattr(tmdb_apply, "tmdb_json", fake_json)
    with app.app_context():
        movie_id = film().id
        diary.review_task(User.query.first().id, MAIN, 4.0)
        db.session.expire_all()
        assert [row.id for row in Movie.query.filter_by(tmdb_id=TMDB_ID)] == [movie_id]
        assert db.session.get(Movie, movie_id).ratings.count() == 1


def test_a_film_that_is_detached_from_tmdb_loses_the_selection(app):
    with app.app_context():
        movie = film()
        movie.tmdb_movie_clear()
        db.session.commit()
        assert movie.chosen_title is None
        assert movie.title == STREAMING


def test_the_fetch_requests_the_alternative_titles(app, monkeypatch):
    from app.metadata import tmdb_apply

    asked = []

    def fake_json(path, params=None, timeout=10):
        asked.append((path, params))
        return payload()

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb_apply, "tmdb_json", fake_json)
    with app.app_context():
        movie = film()
        assert movie.tmdb_movie_fetch(TMDB_ID)["alternative_titles"]
    assert asked[0][0] == f"/movie/{TMDB_ID}"
    assert "alternative_titles" in asked[0][1]["append_to_response"].split(",")


# The page of the admin


@pytest.fixture
def titles(monkeypatch):
    """Give the title page a TMDB payload. Return the dict that holds it."""

    from app.main import movie_pages

    holder = {"payload": payload(alternatives=(STREAMING, "Érase una vez"))}
    monkeypatch.setattr(
        movie_pages, "_tmdb_title_payload", lambda tmdb_id: holder["payload"]
    )
    monkeypatch.setattr(movie_pages, "_wait_for_refresh", lambda job: None)
    return holder


def select(client, movie_id, title, **kwargs):
    """Post a title to the title page of a film, with the token of the session."""

    token = page_csrf_token(client)
    return client.post(
        f"/movie/{movie_id}/title",
        data={"csrf_token": token, "title": title, "title_submit": "Use this title"},
        **kwargs,
    )


def refresh_jobs(app):
    return [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.metadata.tmdb_refresh.refresh_tmdb_info"
    ]


def test_the_title_page_lists_the_titles_of_tmdb(app, admin_client, titles):
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id

    response = admin_client.get(f"/movie/{movie_id}/title")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert STREAMING in page
    assert "Érase una vez" in page
    assert "US: streaming title" in page
    assert "TMDB title" in page


def test_an_admin_selects_an_alternative_title(app, admin_client, titles):
    from app.models import Movie

    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id

    response = select(admin_client, movie_id, STREAMING)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/movie/{movie_id}")
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title == STREAMING
        jobs = refresh_jobs(app)
        assert [job.args for job in jobs] == [("Movies", movie_id, TMDB_ID)]


def test_the_main_title_ends_a_selection(app, admin_client, titles):
    from app.models import Movie

    with app.app_context():
        movie_id = film().id

    response = select(admin_client, movie_id, MAIN)
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None
        assert len(refresh_jobs(app)) == 1


def test_the_stored_selection_has_1_space_for_each_run_of_white_space(
    app, admin_client, titles
):
    """An import reads the title from a file name with each run as 1 space."""

    from app.models import Movie

    titles["payload"] = payload(alternatives=("Once  Upon a\u00a0Time in Hollywood",))
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id

    assert select(admin_client, movie_id, STREAMING).status_code == 302
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title == STREAMING


@pytest.mark.parametrize("hidden", ["...", "?"])
def test_a_title_that_leaves_no_name_for_the_folder_is_refused(
    app, admin_client, titles, hidden
):
    from app.models import Movie

    titles["payload"] = payload(alternatives=(hidden,))
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id

    response = select(admin_client, movie_id, hidden, follow_redirects=True)
    assert "leaves no name for the folder" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None
        assert refresh_jobs(app) == []


def test_a_title_that_tmdb_does_not_list_is_refused(app, admin_client, titles):
    from app.models import Movie

    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id

    response = select(admin_client, movie_id, "My Own Title", follow_redirects=True)
    assert "TMDB does not list" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None
        assert refresh_jobs(app) == []


def test_a_title_that_another_film_of_the_year_has_is_refused(
    app, admin_client, titles
):
    from app.models import Movie

    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id
        make_movie(STREAMING, 2019)
        db.session.commit()

    response = select(admin_client, movie_id, STREAMING, follow_redirects=True)
    assert "has that title and year already" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None
        assert refresh_jobs(app) == []


def test_no_answer_from_tmdb_changes_nothing(app, admin_client, titles):
    from app.models import Movie

    with app.app_context():
        movie_id = film().id
    titles["payload"] = None

    assert admin_client.get(f"/movie/{movie_id}/title").status_code == 200
    response = select(admin_client, movie_id, MAIN, follow_redirects=True)
    assert "could not get the titles" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title == STREAMING
        assert refresh_jobs(app) == []


def test_a_film_with_no_tmdb_id_has_no_title_page(app, admin_client, titles):
    with app.app_context():
        movie_id = make_movie("Home Movie", 1999).id
        db.session.commit()

    response = admin_client.get(f"/movie/{movie_id}/title", follow_redirects=True)
    assert "is not matched to TMDB" in response.get_data(as_text=True)
    response = select(admin_client, movie_id, "Home Movie")
    assert response.status_code == 302
    with app.app_context():
        assert refresh_jobs(app) == []


def test_the_title_page_is_only_for_an_admin(app, user_client, client, titles):
    from app.models import Movie

    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id

    for visitor in (user_client, client):
        assert visitor.get(f"/movie/{movie_id}/title").status_code in (302, 403)
        response = visitor.post(
            f"/movie/{movie_id}/title",
            data={"title": STREAMING, "title_submit": "Use this title"},
        )
        assert response.status_code in (302, 403)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None
        assert refresh_jobs(app) == []


def test_the_film_page_links_to_the_title_page_for_an_admin(app, admin_client):
    with app.app_context():
        movie_id = film().id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert f"/movie/{movie_id}/title" in page
