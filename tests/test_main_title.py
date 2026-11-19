"""Test the main title of TMDB of a film.

A film with a selected title shows that title on the pages. The record
also holds the main title of TMDB. The searches and the frame game find
the film by each of the 2 titles."""

import pytest

from app import db
from tests.factories import make_movie, make_movie_file

SELECTED = "Duck, You Sucker"
MAIN = "A Fistful of Dynamite"
TMDB_ID = 336


def payload(title=MAIN, **extra):
    """Return a details payload that lists the selected title."""

    return {
        "id": TMDB_ID,
        "title": title,
        "original_title": "Giù la testa",
        "release_date": "1971-10-20",
        "alternative_titles": {
            "titles": [{"iso_3166_1": "US", "title": SELECTED, "type": ""}]
        },
        **extra,
    }


def film(selected=SELECTED, **kwargs):
    """Make a film as a refresh leaves it, with the 2 titles."""

    shown = selected or MAIN
    movie = make_movie(
        shown,
        1971,
        tmdb_id=TMDB_ID,
        chosen_title=selected,
        tmdb_title=shown,
        tmdb_main_title=MAIN,
        tmdb_original_title="Giù la testa",
        **kwargs,
    )
    db.session.commit()
    return movie


# The record


def test_a_refresh_stores_the_main_title_beside_the_selected_title(app):
    with app.app_context():
        movie = make_movie(SELECTED, 1971, tmdb_id=TMDB_ID, chosen_title=SELECTED)
        movie.tmdb_movie_apply(payload())
        db.session.commit()
        assert (movie.title, movie.tmdb_title) == (SELECTED, SELECTED)
        assert movie.tmdb_main_title == MAIN

        # An edit of the main title on TMDB changes only the stored main title.
        movie.tmdb_movie_apply(payload(title="A Fistful of Dynamite!"))
        db.session.commit()
        assert (movie.title, movie.tmdb_title) == (SELECTED, SELECTED)
        assert movie.tmdb_main_title == "A Fistful of Dynamite!"


def test_a_film_with_no_selection_has_the_main_title_in_the_2_columns(app):
    with app.app_context():
        movie = make_movie("Old Name", 1971, tmdb_id=TMDB_ID)
        movie.tmdb_movie_apply(payload())
        db.session.commit()
        assert (movie.title, movie.tmdb_title, movie.tmdb_main_title) == (
            MAIN,
            MAIN,
            MAIN,
        )


def test_a_payload_with_no_title_keeps_the_stored_main_title(app):
    with app.app_context():
        movie = film()
        partial = payload()
        del partial["title"]
        movie.tmdb_movie_apply(partial)
        db.session.commit()
        assert (movie.tmdb_title, movie.tmdb_main_title) == (SELECTED, MAIN)


def test_a_main_title_that_is_too_long_is_shortened(app):
    from app.models import Movie

    length = Movie.__table__.c.tmdb_main_title.type.length
    with app.app_context():
        movie = film()
        movie.tmdb_movie_apply(payload(title="Fistful " * 60))
        db.session.commit()
        assert movie.tmdb_main_title == ("Fistful " * 60)[:length]
        assert movie.tmdb_title == SELECTED


def test_a_record_that_loses_its_tmdb_data_loses_the_main_title(app):
    with app.app_context():
        movie = film()
        movie._clear_tmdb()
        db.session.commit()
        assert movie.tmdb_main_title is None


def test_a_new_record_of_a_tmdb_film_starts_with_the_main_title(app):
    from app.metadata.tmdb_refresh import find_or_create_tmdb_movie

    with app.app_context():
        movie, created = find_or_create_tmdb_movie(
            TMDB_ID, MAIN, 1971, details=payload()
        )
        db.session.commit()
        assert created is True
        assert (movie.tmdb_title, movie.tmdb_main_title) == (MAIN, MAIN)


# The searches


def test_the_search_finds_a_film_by_its_main_title(app, admin_client):
    with app.app_context():
        make_movie_file(film(), "Bluray-1080p")
        db.session.commit()

    for query in ("fistful", "sucker"):
        page = admin_client.get(f"/search?q={query}").get_data(as_text=True)
        assert f">{SELECTED} (" in page, query
        data = admin_client.get(f"/search.json?q={query}").get_json()
        assert [row["title"] for row in data["results"]] == [f"{SELECTED} (1971)"]


@pytest.mark.parametrize(
    "query, other_title",
    [
        # The full main title is an exact match. It comes before a prefix match.
        (MAIN, "A Fistful of Dynamite Again"),
        # The start of the main title is a prefix match. It comes before
        # a title that only contains the words.
        ("A Fistful of Dyn", "Before A Fistful of Dynamite"),
    ],
)
def test_a_match_of_the_main_title_has_the_rank_of_its_kind(
    app, admin_client, query, other_title
):
    with app.app_context():
        make_movie_file(film(), "Bluray-1080p")
        make_movie_file(make_movie(other_title, 1972), "Bluray-1080p")
        db.session.commit()

    page = admin_client.get(f"/search?q={query}").get_data(as_text=True)
    selected_at = page.find(f">{SELECTED} (")
    other_at = page.find(f">{other_title} (")
    assert 0 <= selected_at < other_at


def test_the_movie_library_search_finds_a_film_by_its_main_title(app, admin_client):
    with app.app_context():
        make_movie_file(film(), "DVD")
        db.session.commit()

    for query in ("fistful dynamite", "sucker"):
        page = admin_client.get(f"/library/movie?q={query}").get_data(as_text=True)
        assert SELECTED in page, query


def test_the_shopping_search_finds_a_film_by_its_main_title(app, admin_client):
    from app.models import User
    from tests.test_shopping import make_liked_review

    with app.app_context():
        user_id = User.query.first().id
        make_liked_review(user_id, film())
        db.session.commit()

    for query in ("fistful+dynamite", "sucker"):
        page = admin_client.get(f"/shopping-list/movie?q={query}").get_data(
            as_text=True
        )
        assert f"{SELECTED} (1971)" in page, query


def test_the_watchlist_search_finds_a_film_by_its_main_title(app, admin_client):
    from app.models import User, UserWatchlist

    with app.app_context():
        admin_id = User.query.filter_by(admin=True).first().id
        wanted = film()
        other = make_movie("Delta Wanted Other", 1972, tmdb_id=9912)
        db.session.commit()
        for movie in (wanted, other):
            db.session.add(UserWatchlist(user_id=admin_id, movie_id=movie.id))
        db.session.commit()

    for query in ("fistful", "sucker"):
        page = admin_client.get(
            f"/shopping-list/movie?library=watchlist&q={query}"
        ).get_data(as_text=True)
        assert SELECTED in page, query
        assert "Delta Wanted Other" not in page


# The frame game


@pytest.mark.parametrize(
    "guess", [SELECTED, MAIN, "fistful of dynamite", "Giù la testa"]
)
def test_the_game_accepts_each_title_of_a_film(app, guess):
    from app.main.game import _fuzzy_match

    with app.app_context():
        movie = film()
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        assert _fuzzy_match(guess, movie) is True


def test_the_main_title_of_another_film_is_a_better_match_in_the_game(app):
    """A guess that is the main title of a film names that film."""

    from app.main.game import _fuzzy_match

    with app.app_context():
        aliens = make_movie("Aliens", 1986)
        make_movie_file(aliens, "Bluray-1080p")
        other = make_movie(
            "The Eighth Passenger",
            1979,
            tmdb_id=348,
            chosen_title="The Eighth Passenger",
            tmdb_title="The Eighth Passenger",
            tmdb_main_title="Alien",
        )
        make_movie_file(other, "Bluray-1080p")
        db.session.commit()
        assert _fuzzy_match("Alien", other) is True
        assert _fuzzy_match("Alien", aliens) is False
