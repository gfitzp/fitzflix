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


@pytest.mark.parametrize("field", ["title", "original_title"])
def test_a_selection_stands_by_the_main_title_and_by_the_original_title(app, field):
    """The alternative titles are not the only field that counts."""

    details = payload(title="Other Main", alternatives=("Another Name",))
    details["original_title"] = "Other Original"
    details[field] = "Once Upon a Time in Hollywóod"
    with app.app_context():
        movie = film()
        movie.tmdb_movie_apply(details)
        db.session.commit()
        assert (movie.title, movie.chosen_title) == (STREAMING, STREAMING)


def test_a_long_selection_stands_as_the_record_holds_it(app, caplog):
    from app.models import clip_movie_title

    long_title = "Night of the Day of the Dawn " * 8 + "Part 5"
    with app.app_context():
        movie = film(title=clip_movie_title(long_title), chosen=None)
        movie.chosen_title = clip_movie_title(long_title)
        with caplog.at_level(logging.WARNING, logger="app"):
            movie.tmdb_movie_apply(
                payload(title=long_title + " Main", alternatives=(long_title,))
            )
        db.session.commit()
        assert movie.chosen_title == clip_movie_title(long_title)
        assert movie.title == clip_movie_title(long_title)
    assert not [m for m in caplog.messages if "is shortened" in m]


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


def second_applies(app):
    """Return the applies that a merge queued, and no fetch is among the jobs."""

    assert [
        job
        for queue in (app.request_queue, app.sql_queue)
        for job in queue.jobs
        if job.func_name == "app.metadata.tmdb_refresh.refresh_tmdb_info"
    ] == []
    return [
        job.kwargs
        for job in app.sql_queue.jobs
        if job.func_name == "app.metadata.tmdb_refresh.apply_tmdb_refresh"
    ]


def second_apply_targets(app):
    return [(job["library"], job["id"], job["tmdb_id"]) for job in second_applies(app)]


def test_a_merge_gives_the_survivor_the_selection(app):
    """The record that goes holds the name until its delete. A second apply ends the rename."""

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

        assert second_apply_targets(app) == [("Movies", survivor_id, TMDB_ID)]

        # The second apply has the payload of the first. It asks TMDB
        # for nothing.
        assert apply_tmdb_refresh(**second_applies(app)[0]) is True
        db.session.expire_all()
        kept = db.session.get(Movie, survivor_id)
        assert (kept.title, kept.tmdb_title, kept.chosen_title) == (STREAMING,) * 3
        row = db.session.get(File, file_id)
        assert row.file_path.startswith(f"Movies/{STREAMING} (2019)/")
        assert os.path.exists(os.path.join(app.config["LIBRARY_DIR"], row.file_path))
        assert not os.path.exists(path) or path.endswith(row.file_path)
        # The second apply is not a merge. It queues nothing more.
        assert len(second_applies(app)) == 1


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
                tmdb_payload=packed(payload(alternatives=())),
                rematch=True,
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, other_id) is None
        kept = db.session.get(Movie, target_id)
        assert (kept.title, kept.chosen_title) == (MAIN, None)


@pytest.mark.parametrize("alternatives", [(), None, ("Gamma Alt",)])
def test_a_new_tmdb_id_ends_the_selection_of_the_old_film(app, caplog, alternatives):
    """The payload of the new film cannot always tell, and it can list the same title."""

    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import Movie

    with app.app_context():
        movie = make_movie("Gamma Alt", 2019, tmdb_id=77, chosen_title="Gamma Alt")
        movie.tmdb_title = "Gamma Alt"
        db.session.commit()
        movie_id = movie.id
        with caplog.at_level(logging.INFO, logger="app"):
            assert (
                apply_tmdb_refresh(
                    "Movies",
                    movie_id,
                    tmdb_id=TMDB_ID,
                    tmdb_payload=packed(payload(alternatives=alternatives)),
                    rematch=True,
                )
                is True
            )
        db.session.expire_all()
        row = db.session.get(Movie, movie_id)
        assert (row.tmdb_id, row.title, row.chosen_title) == (TMDB_ID, MAIN, None)
    assert [m for m in caplog.messages if "was of TMDB id 77" in m]


def test_a_survivor_keeps_its_own_selection(app):
    from datetime import datetime

    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import Movie

    with app.app_context():
        survivor = film()
        survivor.date_created = datetime(2020, 1, 1)
        newer = make_movie("Newer Record", 2019, tmdb_id=TMDB_ID)
        newer.date_created = datetime(2024, 1, 1)
        db.session.commit()
        survivor_id, newer_id = survivor.id, newer.id
        assert (
            apply_tmdb_refresh(
                "Movies", newer_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is True
        )
        db.session.expire_all()
        kept = db.session.get(Movie, survivor_id)
        assert (kept.title, kept.chosen_title) == (STREAMING, STREAMING)

        # The survivor has the name already. Thus, no second apply.
        assert second_applies(app) == []


def test_a_new_tmdb_id_locks_the_name_of_the_new_film(app, held_lock):
    """The selection of the old film is not the name that the apply gives."""

    from app.metadata.tmdb_refresh import _movie_base_lock, apply_tmdb_refresh
    from app.models import Movie

    with app.app_context():
        movie = make_movie("Gamma Alt", 2019, tmdb_id=77, chosen_title="Gamma Alt")
        db.session.commit()
        movie_id = movie.id
        held_lock(_movie_base_lock(MAIN, 2019))
        assert (
            apply_tmdb_refresh(
                "Movies",
                movie_id,
                tmdb_id=TMDB_ID,
                tmdb_payload=packed(payload(alternatives=())),
                rematch=True,
            )
            is False
        )
        db.session.expire_all()
        row = db.session.get(Movie, movie_id)
        assert (row.title, row.chosen_title) == ("Gamma Alt", "Gamma Alt")


def test_a_merge_with_no_selection_queues_no_second_apply(app):
    from datetime import datetime

    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import Movie

    with app.app_context():
        survivor = make_movie("Older Record", 2019, tmdb_id=TMDB_ID)
        survivor.date_created = datetime(2020, 1, 1)
        newer = make_movie("Newer Record", 2019, tmdb_id=TMDB_ID)
        newer.date_created = datetime(2024, 1, 1)
        db.session.commit()
        newer_id = newer.id
        assert (
            apply_tmdb_refresh(
                "Movies", newer_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, newer_id) is None
        assert second_applies(app) == []


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

        # An id tag names the film. The selection does not replace it.
        make_movie("Tagged Film", 2019, tmdb_id=77)
        db.session.commit()
        tagged = f"{STREAMING} (2019) {{tmdb-77}} - [Bluray-1080p].mkv"
        assert filenames.evaluate_filename(tagged, log=False)["title"] == "Tagged Film"
        assert asked == []

        # A film with no selection still goes through the TMDB search.
        make_movie("Plain Title", 2001, tmdb_id=5)
        db.session.commit()
        filenames.evaluate_filename("Plain Title (2001) - [DVD].mkv", log=False)
        assert asked == ["/search/movie"]


@pytest.mark.parametrize(
    "selection, file_title",
    [
        ("Alpha: Beta", "Alpha Beta"),
        ("What If?", "What If"),
        ("...And Justice", "And Justice"),
        ("Érase una vez", "Érase una vez"),
        ("Érase una vez", "Erase una vez"),
    ],
)
def test_the_name_of_a_file_loses_characters_of_the_selected_title(
    app, monkeypatch, selection, file_title
):
    """The untouched name and the library name of the film both find it."""

    from app.media.importing import filenames

    asked = []
    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        filenames,
        "tmdb_json",
        lambda path, params=None, timeout=10: asked.append(path) or {"results": []},
    )
    with app.app_context():
        film(title=selection, chosen=selection)
        details = filenames.evaluate_filename(
            f"{file_title} (2019) - [Bluray-1080p].mkv", log=False
        )
        assert (details["title"], details["tmdb_id"]) == (selection, TMDB_ID)
        assert asked == []

        # An other year is an other film. It goes through the search.
        filenames.evaluate_filename(
            f"{file_title} (2018) - [Bluray-1080p].mkv", log=False
        )
        assert asked == ["/search/movie"]


def test_the_rename_of_an_other_film_does_not_use_the_selection(app, monkeypatch):
    """A film with no TMDB id gets the main title that an other film selected.

    Its record cannot take the name. Its file must stay in its folder,
    and not go into the folder of the film with the selection."""

    from app.media.importing import filenames
    from app.metadata import tmdb_apply
    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import File

    def fake_json(path, params=None, timeout=10):
        """Answer the title search of the rename with the film of the record."""

        return {
            "results": [{"id": 77, "title": STREAMING, "release_date": "2019-03-01"}]
        }

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(filenames, "tmdb_json", fake_json)
    monkeypatch.setattr(tmdb_apply, "notify_admin", lambda *args, **kwargs: None)
    with app.app_context():
        film()
        other = make_movie("Parsed Name", 2019)
        db.session.commit()
        file = make_movie_file(other, "Bluray-1080p")
        path = on_disk(app, file)
        other_id, file_id, before = other.id, file.id, file.file_path
        details = {
            "id": 77,
            "title": STREAMING,
            "original_title": STREAMING,
            "release_date": "2019-03-01",
            "alternative_titles": {"titles": []},
        }
        assert (
            apply_tmdb_refresh(
                "Movies", other_id, tmdb_id=None, tmdb_payload=packed(details)
            )
            is True
        )
        db.session.expire_all()
        row = db.session.get(File, file_id)
        assert (row.movie_id, row.file_path) == (other_id, before)
        assert os.path.exists(path)


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
    """Each title that the film does not have gets a form. The page posts it."""

    import html
    import re

    from app.models import Movie
    from tests.conftest import csrf_token_from

    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id

    response = admin_client.get(f"/movie/{movie_id}/title")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "TMDB title, Original title" in page
    assert "US: streaming title" in page
    assert page.count("Current</span>") == 1
    assert "Follow the TMDB title" not in page
    offered = [
        html.unescape(value)
        for value in re.findall(r'name="title"[^>]*value="([^"]*)"', page)
    ]
    assert offered == [STREAMING, "Érase una vez"]

    response = admin_client.post(
        f"/movie/{movie_id}/title",
        data={
            "csrf_token": csrf_token_from(page),
            "title": offered[0],
            "title_submit": "Use this title",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title == STREAMING


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
        assert jobs[0].kwargs == {"apply_at_front": True}


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
    assert "TMDB did not answer" in response.get_data(as_text=True)
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
    """The post of a member has the token of the session. Only the role refuses it."""

    from app.models import Movie

    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id

    assert user_client.get(f"/movie/{movie_id}/title").status_code in (302, 403)
    assert select(user_client, movie_id, STREAMING).status_code in (302, 403)
    assert client.get(f"/movie/{movie_id}/title").status_code in (302, 401, 403)
    response = client.post(
        f"/movie/{movie_id}/title",
        data={"title": STREAMING, "title_submit": "Use this title"},
    )
    assert response.status_code in (302, 401, 403)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None
        assert refresh_jobs(app) == []


def test_the_film_page_links_to_the_title_page_for_an_admin(app, admin_client):
    with app.app_context():
        movie_id = film().id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert f"/movie/{movie_id}/title" in page


def test_the_title_page_asks_tmdb_for_the_alternative_titles(app, monkeypatch):
    from app.main import movie_pages
    from app.metadata import tmdb_apply

    asked = []

    def fake_json(path, params=None, timeout=10):
        asked.append((path, params))
        return payload()

    monkeypatch.setattr(tmdb_apply, "tmdb_json", fake_json)
    with app.app_context():
        assert movie_pages._tmdb_title_payload(TMDB_ID) == payload()
    assert asked == [
        (f"/movie/{TMDB_ID}", {"append_to_response": "alternative_titles"})
    ]


def test_the_button_of_the_page_ends_a_selection_that_is_the_main_title_now(
    app, admin_client, titles
):
    """The row of the TMDB title has no button then."""

    from app.models import Movie

    titles["payload"] = payload(title=STREAMING, alternatives=(MAIN,))
    with app.app_context():
        movie_id = film().id

    page = admin_client.get(f"/movie/{movie_id}/title").get_data(as_text=True)
    assert "Follow the TMDB title" in page
    assert f'value="{STREAMING}"' not in page
    response = admin_client.post(
        f"/movie/{movie_id}/title",
        data={
            "csrf_token": page_csrf_token(admin_client),
            "follow_submit": "Follow the TMDB title",
        },
        follow_redirects=True,
    )
    assert "follows the TMDB title again" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None
        assert len(refresh_jobs(app)) == 1


def test_the_holder_check_uses_the_year_of_tmdb(app, admin_client, titles):
    """The refresh gives the film the year of TMDB with the title."""

    from app.models import Movie

    titles["payload"] = payload(release_date="2020-02-02")
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id
        make_movie(STREAMING, 2020)
        db.session.commit()

    response = select(admin_client, movie_id, STREAMING, follow_redirects=True)
    assert "has that title and year already" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None


def test_a_film_can_select_the_title_that_its_record_has(app, admin_client, titles):
    """The film itself is not an other film with the title."""

    from app.models import Movie

    with app.app_context():
        movie_id = film(chosen=None).id

    assert select(admin_client, movie_id, STREAMING).status_code == 302
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title == STREAMING
        assert len(refresh_jobs(app)) == 1


def test_a_long_selection_is_stored_as_the_record_holds_a_title(
    app, admin_client, titles
):
    from app.models import Movie, clip_movie_title

    long_title = "Night of the Day of the Dawn " * 8 + "Part 5"
    titles["payload"] = payload(alternatives=(long_title,))
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id

    assert select(admin_client, movie_id, long_title).status_code == 302
    with app.app_context():
        stored = db.session.get(Movie, movie_id).chosen_title
        assert stored == clip_movie_title(long_title)
        assert len(stored) <= Movie.__table__.c.title.type.length


def test_a_selection_that_the_record_does_not_have_keeps_its_button(
    app, admin_client, titles
):
    """An other record held the name, so the record did not take the selection."""

    with app.app_context():
        movie = film(title=MAIN, chosen=STREAMING)
        movie.tmdb_title = STREAMING
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}/title").get_data(as_text=True)
    assert f'value="{STREAMING}"' in page
    assert f'value="{MAIN}"' not in page


def follow(client, movie_id, **kwargs):
    return client.post(
        f"/movie/{movie_id}/title",
        data={
            "csrf_token": page_csrf_token(client),
            "follow_submit": "Follow the TMDB title",
        },
        **kwargs,
    )


def test_the_button_changes_nothing_with_no_answer_from_tmdb(app, admin_client, titles):
    from app.models import Movie

    with app.app_context():
        movie_id = film().id
    titles["payload"] = None

    response = follow(admin_client, movie_id, follow_redirects=True)
    assert "TMDB did not answer" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title == STREAMING
        assert refresh_jobs(app) == []


def test_the_button_is_refused_when_another_film_has_the_tmdb_title(
    app, admin_client, titles
):
    from app.models import Movie

    with app.app_context():
        movie_id = film().id
        make_movie(MAIN, 2019)
        db.session.commit()

    response = follow(admin_client, movie_id, follow_redirects=True)
    assert "has that title and year already" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title == STREAMING
        assert refresh_jobs(app) == []


def test_the_button_does_nothing_for_a_film_with_no_selection(
    app, admin_client, titles
):
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id

    response = follow(admin_client, movie_id, follow_redirects=True)
    assert "follows the TMDB title already" in response.get_data(as_text=True)
    with app.app_context():
        assert refresh_jobs(app) == []


def test_a_selection_on_a_younger_record_goes_to_the_page_of_the_oldest(
    app, admin_client, titles
):
    """The refresh merges the younger record into the oldest one."""

    from datetime import datetime

    with app.app_context():
        oldest = make_movie("Oldest Record", 2019, tmdb_id=TMDB_ID)
        oldest.date_created = datetime(2020, 1, 1)
        younger = film(title=MAIN, chosen=None)
        younger.date_created = datetime(2024, 1, 1)
        db.session.commit()
        oldest_id, younger_id = oldest.id, younger.id

    response = select(admin_client, younger_id, STREAMING)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/movie/{oldest_id}")
    # The link of the message goes to the same record.
    page = admin_client.get(f"/movie/{oldest_id}").get_data(as_text=True)
    assert f"href=&#39;/movie/{oldest_id}&#39;" in page or (
        f"href='/movie/{oldest_id}'" in page
    )
    assert f"href='/movie/{younger_id}'" not in page
    with app.app_context():
        assert [job.args for job in refresh_jobs(app)] == [
            ("Movies", younger_id, TMDB_ID)
        ]


def test_a_long_title_that_the_record_has_gets_the_badge(app, admin_client, titles):
    """The record holds a long title in its shortened form."""

    from app.models import clip_movie_title

    long_title = "Night of the Day of the Dawn " * 8 + "Part 5"
    titles["payload"] = payload(title=long_title)
    with app.app_context():
        movie = film(title=clip_movie_title(long_title), chosen=None)
        movie.tmdb_title = long_title
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}/title").get_data(as_text=True)
    assert page.count("Current</span>") == 1


def test_the_file_name_of_a_selection_has_the_year_of_tmdb(app, monkeypatch):
    """The untouched name has the year of the release date, as the rename gives it."""

    from datetime import datetime

    from app.media.importing import filenames

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        filenames, "tmdb_json", lambda path, params=None, timeout=10: {"results": []}
    )
    with app.app_context():
        movie = film(title="Record Title", chosen=STREAMING, year=2018)
        movie.tmdb_title = STREAMING
        movie.tmdb_release_date = datetime(2019, 7, 24)
        db.session.commit()
        details = filenames.evaluate_filename(
            f"{STREAMING} (2019) - [Bluray-1080p].mkv", log=False
        )
        assert details["tmdb_id"] == TMDB_ID


# A selection whose file name is the file name of another film


@pytest.mark.parametrize(
    "other_title, selection",
    [
        ("Alpha Beta", "Alpha: Beta"),
        ("Alpha: Beta", "Alpha Beta"),
        ("What If", "What If?"),
        ("Erase una vez", "Érase una vez"),
        ("alpha beta", "Alpha: Beta"),
    ],
)
def test_a_title_with_the_file_name_of_another_film_is_refused(
    app, admin_client, titles, other_title, selection
):
    """A file with no id tag has the same name for the 2 films."""

    from app.models import Movie

    titles["payload"] = payload(alternatives=(selection,))
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id
        make_movie(other_title, 2019, tmdb_id=77)
        db.session.commit()

    response = select(admin_client, movie_id, selection, follow_redirects=True)
    assert "gives its files the same name" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None
        assert refresh_jobs(app) == []


def test_the_tmdb_title_of_another_film_counts_for_the_file_name(
    app, admin_client, titles
):
    """The untouched name of a file has the TMDB title and the TMDB year."""

    from datetime import datetime

    from app.models import Movie

    titles["payload"] = payload(alternatives=("Alpha: Beta",))
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id
        other = make_movie("A Record Title", 2018, tmdb_id=77)
        other.tmdb_title = "Alpha Beta"
        other.tmdb_release_date = datetime(2019, 5, 5)
        db.session.commit()

    response = select(admin_client, movie_id, "Alpha: Beta", follow_redirects=True)
    assert "gives its files the same name" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None


def test_a_film_of_another_year_or_of_the_same_tmdb_id_does_not_refuse(
    app, admin_client, titles
):
    """An other year gives an other name. A record of the same film is merged."""

    from app.models import Movie

    titles["payload"] = payload(alternatives=("Alpha: Beta",))
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id
        make_movie("Alpha Beta", 2018, tmdb_id=77)
        make_movie("Alpha Beta", 2019, tmdb_id=TMDB_ID)
        db.session.commit()

    assert select(admin_client, movie_id, "Alpha: Beta").status_code == 302
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title == "Alpha: Beta"
        assert len(refresh_jobs(app)) == 1


def test_a_film_that_has_the_name_of_the_file_comes_before_a_selection(
    app, monkeypatch
):
    """The other film came after the selection. Its record has the name of the file."""

    from app.media.importing import filenames

    asked = []

    def fake_json(path, params=None, timeout=10):
        asked.append(path)
        return {
            "results": [{"id": 77, "title": "Alpha Beta", "release_date": "2019-01-01"}]
        }

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(filenames, "tmdb_json", fake_json)
    with app.app_context():
        film(title="Alpha: Beta", chosen="Alpha: Beta")
        name = "Alpha Beta (2019) - [Bluray-1080p].mkv"
        details = filenames.evaluate_filename(name, log=False)
        assert details["tmdb_id"] == TMDB_ID
        assert asked == []

        make_movie("Alpha Beta", 2019, tmdb_id=77)
        db.session.commit()
        details = filenames.evaluate_filename(name, log=False)
        assert details["title"] == "Alpha Beta"
        assert details["tmdb_id"] != TMDB_ID
        assert asked == ["/search/movie"]


def test_only_a_film_with_the_title_and_the_year_of_the_file_comes_first(
    app, monkeypatch
):
    """An other title of the year, and the same title of an other year, change nothing."""

    from app.media.importing import filenames

    asked = []
    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        filenames,
        "tmdb_json",
        lambda path, params=None, timeout=10: asked.append(path) or {"results": []},
    )
    with app.app_context():
        film(title="Alpha: Beta", chosen="Alpha: Beta")
        make_movie("An Other Film", 2019, tmdb_id=77)
        make_movie("Alpha Beta", 2018, tmdb_id=78)
        db.session.commit()
        details = filenames.evaluate_filename(
            "Alpha Beta (2019) - [Bluray-1080p].mkv", log=False
        )
        assert details["tmdb_id"] == TMDB_ID
        assert asked == []


def test_a_film_with_no_tmdb_id_and_the_same_file_name_refuses(
    app, admin_client, titles
):
    from app.models import Movie

    titles["payload"] = payload(alternatives=("Alpha: Beta",))
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None).id
        make_movie("Alpha Beta", 2019)
        db.session.commit()

    response = select(admin_client, movie_id, "Alpha: Beta", follow_redirects=True)
    assert "gives its files the same name" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None


def test_the_file_name_check_uses_the_year_of_tmdb(app, admin_client, titles):
    """The refresh gives the film the year of TMDB. The name has that year."""

    from app.models import Movie

    titles["payload"] = payload(alternatives=("Alpha: Beta",))
    with app.app_context():
        movie_id = film(title=MAIN, chosen=None, year=2018).id
        make_movie("Alpha Beta", 2019, tmdb_id=77)
        db.session.commit()

    response = select(admin_client, movie_id, "Alpha: Beta", follow_redirects=True)
    assert "gives its files the same name" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None


def test_the_tmdb_title_is_not_checked_for_the_file_name(app, admin_client, titles):
    """A refresh gives a film with no selection the TMDB title in each case."""

    from app.models import Movie

    titles["payload"] = payload(title="Alpha: Beta", alternatives=(STREAMING,))
    with app.app_context():
        movie_id = film().id
        make_movie("Alpha Beta", 2019, tmdb_id=77)
        db.session.commit()

    response = follow(admin_client, movie_id, follow_redirects=True)
    assert "follows the TMDB title again" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, movie_id).chosen_title is None
        assert len(refresh_jobs(app)) == 1


# 2 records of 1 film


def two_records(older_title, younger_title, older_chosen=None, younger_chosen=None):
    """Make 2 records with the TMDB id of the film. Return their ids."""

    from datetime import datetime

    older = make_movie(older_title, 2019, tmdb_id=TMDB_ID, chosen_title=older_chosen)
    older.tmdb_title = older_title
    older.date_created = datetime(2020, 1, 1)
    younger = make_movie(
        younger_title, 2019, tmdb_id=TMDB_ID, chosen_title=younger_chosen
    )
    younger.tmdb_title = younger_title
    younger.date_created = datetime(2024, 1, 1)
    db.session.commit()
    return older.id, younger.id


@pytest.fixture
def mails(monkeypatch):
    """Record the mails that the apply sends to the admin."""

    from app.metadata import tmdb_apply

    sent = []
    monkeypatch.setattr(
        tmdb_apply, "notify_admin", lambda subject, *a, **k: sent.append(subject)
    )
    return sent


def test_the_older_record_of_the_film_can_have_the_selected_title(
    app, admin_client, titles
):
    """The refresh of the younger record merges it into the older record."""

    from app.models import Movie

    with app.app_context():
        older_id, younger_id = two_records(STREAMING, MAIN)

    response = select(admin_client, younger_id, STREAMING, follow_redirects=True)
    page = response.get_data(as_text=True)
    assert "has that title and year already" not in page
    assert "Selected the title" in page
    with app.app_context():
        assert db.session.get(Movie, older_id).chosen_title == STREAMING
        assert db.session.get(Movie, younger_id).chosen_title == STREAMING
        assert [job.args for job in refresh_jobs(app)] == [
            ("Movies", younger_id, TMDB_ID)
        ]


def test_the_younger_record_of_the_film_can_have_the_selected_title(
    app, admin_client, titles
):
    """Only the refresh of a younger record merges. Thus, that record is refreshed."""

    from app.models import Movie

    with app.app_context():
        older_id, younger_id = two_records(MAIN, STREAMING)

    response = select(admin_client, older_id, STREAMING)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/movie/{older_id}")
    with app.app_context():
        assert db.session.get(Movie, older_id).chosen_title == STREAMING
        assert db.session.get(Movie, younger_id).chosen_title == STREAMING
        assert [job.args for job in refresh_jobs(app)] == [
            ("Movies", younger_id, TMDB_ID)
        ]


def test_the_button_ends_the_selection_of_each_record_of_the_film(
    app, admin_client, titles
):
    """A selection that stays on a record would go to the survivor of the merge."""

    from app.models import Movie

    with app.app_context():
        older_id, younger_id = two_records(
            STREAMING, MAIN, older_chosen=STREAMING, younger_chosen="Érase una vez"
        )

    response = follow(admin_client, older_id, follow_redirects=True)
    page = response.get_data(as_text=True)
    assert "has that title and year already" not in page
    assert "follows the TMDB title again" in page
    with app.app_context():
        assert db.session.get(Movie, older_id).chosen_title is None
        assert db.session.get(Movie, younger_id).chosen_title is None
        assert [job.args for job in refresh_jobs(app)] == [
            ("Movies", younger_id, TMDB_ID)
        ]


def test_a_new_selection_replaces_the_selection_of_each_record(
    app, admin_client, titles
):
    """The survivor of a merge keeps its own selection. Thus, it gets the new one."""

    from app.models import Movie

    with app.app_context():
        older_id, younger_id = two_records(
            "Érase una vez", MAIN, older_chosen="Érase una vez"
        )

    assert select(admin_client, younger_id, STREAMING).status_code == 302
    with app.app_context():
        assert db.session.get(Movie, older_id).chosen_title == STREAMING
        assert db.session.get(Movie, younger_id).chosen_title == STREAMING


def test_a_third_record_of_the_film_that_has_the_title_is_refreshed(
    app, admin_client, titles
):
    from datetime import datetime

    with app.app_context():
        older_id, younger_id = two_records("Oldest Record", MAIN)
        third = make_movie(STREAMING, 2019, tmdb_id=TMDB_ID)
        third.date_created = datetime(2025, 1, 1)
        db.session.commit()
        third_id = third.id

    response = select(admin_client, younger_id, STREAMING)
    assert response.headers["Location"].endswith(f"/movie/{older_id}")
    with app.app_context():
        assert [job.args for job in refresh_jobs(app)] == [
            ("Movies", third_id, TMDB_ID)
        ]


@pytest.mark.parametrize("other_tmdb_id", [None, 77])
def test_a_record_of_another_film_that_has_the_title_refuses(
    app, admin_client, titles, other_tmdb_id
):
    from app.models import Movie

    with app.app_context():
        older_id, younger_id = two_records("Oldest Record", MAIN)
        make_movie(STREAMING, 2019, tmdb_id=other_tmdb_id)
        db.session.commit()

    response = select(admin_client, younger_id, STREAMING, follow_redirects=True)
    assert "has that title and year already" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Movie, older_id).chosen_title is None
        assert db.session.get(Movie, younger_id).chosen_title is None
        assert refresh_jobs(app) == []


def test_a_merge_sends_no_mail_about_the_record_that_it_deletes(app, mails):
    """The record that goes has the name. A second apply gives it to the survivor."""

    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import Movie

    with app.app_context():
        older_id, younger_id = two_records(
            MAIN, STREAMING, older_chosen=STREAMING, younger_chosen=STREAMING
        )
        assert (
            apply_tmdb_refresh(
                "Movies", younger_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, younger_id) is None
        assert mails == []
        assert second_apply_targets(app) == [("Movies", older_id, TMDB_ID)]

        assert apply_tmdb_refresh(**second_applies(app)[0]) is True
        db.session.expire_all()
        kept = db.session.get(Movie, older_id)
        assert (kept.title, kept.chosen_title) == (STREAMING, STREAMING)
        assert mails == []
        assert len(second_applies(app)) == 1


def test_a_merge_gives_the_survivor_the_tmdb_title_of_the_record_that_goes(app, mails):
    """The same applies to a film with no selection."""

    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import File, Movie

    with app.app_context():
        older_id, younger_id = two_records("Older Record", MAIN)
        file = make_movie_file(db.session.get(Movie, younger_id), "Bluray-1080p")
        on_disk(app, file)
        file_id = file.id
        assert (
            apply_tmdb_refresh(
                "Movies", younger_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, younger_id) is None
        assert db.session.get(Movie, older_id).title == "Older Record"
        assert mails == []
        assert second_apply_targets(app) == [("Movies", older_id, TMDB_ID)]

        assert apply_tmdb_refresh(**second_applies(app)[0]) is True
        db.session.expire_all()
        assert db.session.get(Movie, older_id).title == MAIN
        row = db.session.get(File, file_id)
        assert row.movie_id == older_id
        assert os.path.exists(os.path.join(app.config["LIBRARY_DIR"], row.file_path))
        assert mails == []
        assert len(second_applies(app)) == 1


def test_a_third_record_that_has_the_name_gets_the_mail_and_no_second_apply(app, mails):
    """The merge does not delete that record. Thus, a second apply cannot help."""

    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import Movie

    with app.app_context():
        older_id, younger_id = two_records(
            "Older Record", "Newer Record", older_chosen=STREAMING
        )
        make_movie(STREAMING, 2019)
        db.session.commit()
        assert (
            apply_tmdb_refresh(
                "Movies", younger_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, younger_id) is None
        assert db.session.get(Movie, older_id).title == "Older Record"
        assert mails == ["Duplicate movie detected"]
        assert second_applies(app) == []


def test_a_refresh_that_is_not_a_merge_sends_the_mail_about_the_other_record(
    app, mails
):
    """The older record is not merged by its own refresh. The admin must know."""

    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import Movie

    with app.app_context():
        older_id, younger_id = two_records("Older Record", MAIN)
        assert (
            apply_tmdb_refresh(
                "Movies", older_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, younger_id) is not None
        assert db.session.get(Movie, older_id).title == "Older Record"
        assert mails == ["Duplicate movie detected"]


def test_a_record_that_goes_with_the_title_and_an_other_year_does_not_hold_the_name(
    app, mails
):
    """The title and the year together are the name. The survivor takes it at once."""

    from app.metadata.tmdb_refresh import apply_tmdb_refresh
    from app.models import Movie

    with app.app_context():
        older_id, younger_id = two_records("Older Record", MAIN)
        db.session.get(Movie, younger_id).year = 2018
        db.session.commit()
        assert (
            apply_tmdb_refresh(
                "Movies", younger_id, tmdb_id=TMDB_ID, tmdb_payload=packed(payload())
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, younger_id) is None
        kept = db.session.get(Movie, older_id)
        assert (kept.title, kept.year) == (MAIN, 2019)
        assert mails == []
        assert second_applies(app) == []
