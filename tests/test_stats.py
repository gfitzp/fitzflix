"""Test the viewing statistics page (#264)."""

from datetime import datetime

from app import db
from app.models import (
    MovieCast,
    MovieCrew,
    TMDBCredit,
    TMDBProductionCountry,
    User,
    UserMovieReview,
)
from tests.factories import make_movie


def viewing(user_id, movie, watched, rating=None, rewatch=None):
    db.session.add(
        UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            rating=rating,
            rewatch=rewatch,
            date_watched=watched,
            date_reviewed=watched or datetime(2025, 12, 1),
        )
    )


def build_diary(app):
    """Make 3 films with credits and countries, and 5 diary rows.

    Director Dee made A (1959) and B (1962). Director Ess made C (1985).
    Lead is the first billed of A. Walk-on is billed 12th. A was watched
    in 2024 and again in 2025. A rating with no date is not a viewing."""

    with app.app_context():
        user_id = User.query.filter_by(admin=True).one().id
        dee = TMDBCredit(id=91001, name="Dee Director")
        ess = TMDBCredit(id=91002, name="Ess Director")
        lead = TMDBCredit(id=91003, name="Lead Actor")
        walk_on = TMDBCredit(id=91004, name="Walk-on Actor")
        france = TMDBProductionCountry(id="FR", name="France")
        usa = TMDBProductionCountry(id="US", name="United States of America")
        db.session.add_all([dee, ess, lead, walk_on, france, usa])

        a = make_movie("Film A", 1959, tmdb_runtime=100)
        b = make_movie("Film B", 1962, tmdb_runtime=90)
        c = make_movie("Film C", 1985, tmdb_runtime=120)
        for movie, director in ((a, dee), (b, dee), (c, ess)):
            db.session.add(
                MovieCrew(
                    movie_id=movie.id,
                    credit_id=director.id,
                    department="Directing",
                    job="Director",
                )
            )
        db.session.add(MovieCast(movie_id=a.id, credit_id=lead.id, billing_order=0))
        db.session.add(MovieCast(movie_id=a.id, credit_id=walk_on.id, billing_order=12))
        a.production_countries.append(france)
        b.production_countries.append(france)
        b.production_countries.append(usa)
        c.production_countries.append(usa)

        viewing(user_id, a, datetime(2024, 3, 1, 20), 4.0, False)
        viewing(user_id, a, datetime(2025, 1, 10, 20), 5.0, True)
        viewing(user_id, b, datetime(2025, 1, 20, 21), 3.5, False)
        viewing(user_id, c, datetime(2025, 6, 2, 19))
        viewing(user_id, c, None, 2.0)
        db.session.commit()
        return user_id, a.id


def test_year_stats(app):
    from app.stats import diary_stats, watched_years

    user_id, _ = build_diary(app)
    with app.app_context():
        assert watched_years(user_id) == [2025, 2024]
        stats = diary_stats(user_id, 2025)

    assert stats["totals"] == {
        "viewings": 3,
        "films": 3,
        "hours": 5,
        "first_watches": 1,
        "rewatches": 1,
        "rated": 2,
        "average": 4.2,
    }
    months = {p["label"]: p["count"] for p in stats["periods"]}
    assert len(months) == 12
    assert (months["Jan"], months["Jun"], months["Mar"]) == (2, 1, 0)
    assert stats["periods_max"] == 2

    ratings = {r["label"]: r["count"] for r in stats["ratings"]}
    assert (ratings["5"], ratings["3½"], ratings["2"]) == (1, 1, 0)

    assert [(d["name"], d["viewings"], d["films"]) for d in stats["directors"]] == [
        ("Dee Director", 2, 2),
        ("Ess Director", 1, 1),
    ]
    # The walk-on part is past the billing cut.
    assert [c["name"] for c in stats["cast"]] == ["Lead Actor"]
    # A tie goes to the name in alphabetical order.
    assert [(c["name"], c["viewings"]) for c in stats["countries"]] == [
        ("France", 2),
        ("United States of America", 2),
    ]
    # Each decade between the first and the last has a column.
    assert [(d["label"], d["count"]) for d in stats["decades"]] == [
        ("1950s", 1),
        ("1960s", 1),
        ("1970s", 0),
        ("1980s", 1),
    ]
    assert stats["most_watched"] == []


def test_all_time_stats(app):
    from datetime import date

    from app.stats import diary_stats

    user_id, a_id = build_diary(app)
    with app.app_context():
        stats = diary_stats(user_id)

    assert stats["totals"]["viewings"] == 4
    assert stats["totals"]["films"] == 3
    years = {p["title"]: p["count"] for p in stats["periods"]}
    assert (years["2024"], years["2025"]) == (1, 3)
    assert max(int(y) for y in years) == date.today().year
    assert stats["most_watched"] == [
        {"id": a_id, "title": "Film A", "year": 1959, "viewings": 2}
    ]


def test_star_label():
    from app.stats import star_label

    assert [star_label(r) for r in (0.5, 1.0, 3.5, 5.0)] == ["½", "1", "3½", "5"]


def test_stats_page(app, admin_client, user_client):
    build_diary(app)

    # Without a year, the page shows the newest year with viewings.
    page = admin_client.get("/stats").get_data(as_text=True)
    assert 'aria-current="page">2025<' in page
    assert "Films by month" in page
    assert 'href="/library/movie?credit=91001"' in page
    assert "Walk-on Actor" not in page

    page = admin_client.get("/stats?year=all").get_data(as_text=True)
    assert "Films by year" in page
    assert "Film A (1959)" in page and "2 times" in page

    # A year with no viewings falls back to the newest year.
    page = admin_client.get("/stats?year=1999").get_data(as_text=True)
    assert 'aria-current="page">2025<' in page

    # A user with no diary sees the empty state.
    page = user_client.get("/stats").get_data(as_text=True)
    assert "No dated viewings yet" in page
    assert "My Stats" in page
