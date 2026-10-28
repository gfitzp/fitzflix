"""Test the free-text grading of the frame game against near misses (#491).

Each pair below exists in the library. A one-character difference, a
sequel number, or a year names a different film. An original title
names the film."""

import pytest

from tests.factories import make_movie, make_movie_file

LIBRARY = [
    ("Alien", 1979, {}),
    ("Aliens", 1986, {}),
    ("Kill Bill: Vol. 1", 2003, {}),
    ("Kill Bill: Vol. 2", 2004, {}),
    ("Iron Man 2", 2010, {}),
    ("Iron Man 3", 2013, {}),
    ("The Hunger Games: Mockingjay - Part 1", 2014, {}),
    ("The Hunger Games: Mockingjay - Part 2", 2015, {}),
    ("Dead Ringer", 1964, {}),
    ("Dead Ringers", 1988, {}),
    ("Hoot", 2006, {}),
    ("Hook", 1991, {}),
    ("Troy", 2004, {}),
    ("Tron", 1982, {}),
    ("The Rink", 1916, {}),
    ("The Ring", 2002, {}),
    ("Nosferatu", 1922, {}),
    ("Nosferatu", 1979, {}),
    ("Cape Fear", 1962, {}),
    ("Cape Fear", 1991, {}),
    ("Batman", 1966, {}),
    ("Batman", 1989, {}),
    ("Bicycle Thieves", 1948, {"tmdb_original_title": "Ladri di biciclette"}),
    ("The Taking of Pelham One Two Three", 1974, {}),
    ("John Wick: Chapter 2", 2017, {}),
    ("The Godfather Part II", 1974, {}),
    ("Malcolm X", 1992, {}),
    ("American History X", 1998, {}),
]


@pytest.fixture
def library(app):
    from app import db

    with app.app_context():
        movies = {}
        for title, year, extra in LIBRARY:
            movie = make_movie(title, year, **extra)
            make_movie_file(movie, "Bluray-1080p")
            movies[(title, year)] = movie.id
        db.session.commit()
        yield movies


@pytest.mark.parametrize(
    "guess, answer, correct",
    [
        ("Aliens", ("Alien", 1979), False),
        ("Alien", ("Aliens", 1986), False),
        ("Alien", ("Alien", 1979), True),
        ("Kill Bill: Vol. 1", ("Kill Bill: Vol. 2", 2004), False),
        ("kill bill vol 2", ("Kill Bill: Vol. 2", 2004), True),
        ("Iron Man 2", ("Iron Man 3", 2013), False),
        ("iron man three", ("Iron Man 3", 2013), True),
        ("Mockingjay Part 1", ("The Hunger Games: Mockingjay - Part 2", 2015), False),
        ("Mockingjay Part 2", ("The Hunger Games: Mockingjay - Part 2", 2015), True),
        ("Dead Ringer", ("Dead Ringers", 1988), False),
        ("Hoot", ("Hook", 1991), False),
        ("Troy", ("Tron", 1982), False),
        ("The Rink", ("The Ring", 2002), False),
        ("Nosferatu (1922)", ("Nosferatu", 1979), False),
        ("Nosferatu (1979)", ("Nosferatu", 1979), True),
        ("Cape Fear 1962", ("Cape Fear", 1991), False),
        ("Cape Fear", ("Cape Fear", 1991), True),
        ("Batman (1966)", ("Batman", 1989), False),
        ("Ladri di biciclette", ("Bicycle Thieves", 1948), True),
        (
            "The Taking of Pelham 123",
            ("The Taking of Pelham One Two Three", 1974),
            True,
        ),
    ],
)
def test_the_grade_of_each_near_miss(app, library, guess, answer, correct):
    from app import db
    from app.main.game import _fuzzy_match
    from app.models import Movie

    with app.app_context():
        movie = db.session.get(Movie, library[answer])
        assert _fuzzy_match(guess, movie) is correct
