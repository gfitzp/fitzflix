"""Test that record creation from TMDB shortens a long title (#462).

MariaDB refuses a value longer than Movie.title with error 1406. SQLite
does not check the length. Thus, the tests check the stored length."""

from app import db

LONG = "Night of the Day of the Dawn " * 8 + "Part 5"


def test_a_long_tmdb_title_is_shortened_at_creation(app):
    from app.models import Movie
    from app.tmdb_refresh import find_or_create_tmdb_movie

    length = Movie.__table__.c.title.type.length
    assert len(LONG) > length
    with app.app_context():
        movie, created = find_or_create_tmdb_movie(
            946201,
            LONG,
            1991,
            details={"title": LONG, "release_date": "1991-01-01"},
        )
        db.session.commit()
        assert created
        assert len(movie.title) <= length
        assert movie.title == LONG[:length].rstrip()
        assert movie.tmdb_title == LONG


def test_a_shortened_title_finds_the_same_record(app):
    """A row with the shortened title and no TMDB id gets the id, not a twin."""

    from app.models import Movie, clip_movie_title
    from app.tmdb_refresh import find_or_create_tmdb_movie

    with app.app_context():
        existing = Movie(title=clip_movie_title(LONG), year=1992)
        db.session.add(existing)
        db.session.commit()
        movie, created = find_or_create_tmdb_movie(946202, LONG, 1992)
        assert not created
        assert movie.id == existing.id
        assert movie.tmdb_id == 946202


def test_a_short_title_is_kept(app):
    from app.models import clip_movie_title

    with app.app_context():
        assert clip_movie_title("Jaws") == "Jaws"
        assert clip_movie_title(None) is None
