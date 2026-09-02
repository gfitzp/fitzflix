"""Test the all-people page.

The page lists each person with a credit in the films of the library.
This includes the cast and the key crew roles. By default, it shows
only the people with more than 1 film. A role that is only uncredited
never counts. This is the specification from Glenn in GitHub #13.
"""

from app import db
from app.models import MovieCast, MovieCrew, TMDBCredit
from tests.factories import make_movie


def make_person(person_id, name, movies, character="Self"):
    person = TMDBCredit(id=person_id, name=name)
    db.session.add(person)
    db.session.flush()
    for order, movie in enumerate(movies):
        db.session.add(
            MovieCast(
                movie_id=movie.id,
                credit_id=person.id,
                character=character,
                billing_order=order,
            )
        )
    return person


def make_crew_person(person_id, name, jobs_by_movie):
    """Build a TMDBCredit with crew rows: {movie: [(department, job), ...]}."""

    person = TMDBCredit(id=person_id, name=name)
    db.session.add(person)
    db.session.flush()
    for movie, jobs in jobs_by_movie.items():
        for department, job in jobs:
            db.session.add(
                MovieCrew(
                    movie_id=movie.id,
                    credit_id=person.id,
                    department=department,
                    job=job,
                )
            )
    return person


def build_population(app):
    first = make_movie("People Film One", 1970)
    second = make_movie("People Film Two", 1971)
    make_person(801, "Repertory Regular", [first, second])
    make_person(802, "One Scene Wonder", [first])
    make_person(
        803, "Always Uncredited", [first, second], character="Extra (uncredited)"
    )
    make_person(804, "No Character Listed", [first, second], character=None)
    db.session.commit()


def test_people_page_defaults_to_multi_film_credited_people(app, admin_client):
    with app.app_context():
        build_population(app)

    page = admin_client.get("/people").get_data(as_text=True)
    assert "Repertory Regular" in page
    assert "credit=801" in page
    assert "2 titles" in page

    # People with 1 film are the long tail. They appear only through the search.
    assert "One Scene Wonder" not in page

    # A role that is only uncredited never counts for the filter.
    assert "Always Uncredited" not in page

    # A missing character string is not the same as uncredited.
    assert "No Character Listed" in page


def test_people_search_widens_to_single_film_people(app, admin_client):
    with app.app_context():
        build_population(app)

    page = admin_client.get("/people?q=Scene+Wonder").get_data(as_text=True)
    assert "One Scene Wonder" in page
    assert "1 title<" in page or "1 title\n" in page or "1 title</span>" in page
    assert "Repertory Regular" not in page


def test_people_ordered_by_film_count(app, admin_client):
    with app.app_context():
        movies = [make_movie(f"Count Film {n}", 1980 + n) for n in range(4)]
        make_person(811, "Busy Actor", movies)
        make_person(812, "Occasional Actor", movies[:2])
        db.session.commit()

    page = admin_client.get("/people").get_data(as_text=True)
    assert page.index("Busy Actor") < page.index("Occasional Actor")
    assert "4 titles" in page


def test_people_counts_key_crew_roles_with_role_badges(app, admin_client):
    """Include the key crew roles in the film-count order with a dominant-role label.

    A job that is not a key role never counts. Glenn: grips and gaffers
    must not outrank directors."""

    with app.app_context():
        movies = [make_movie(f"Crew Film {n}", 1970 + n) for n in range(3)]
        make_crew_person(
            831,
            "Steady Director",
            {movie: [("Directing", "Director")] for movie in movies},
        )
        make_crew_person(
            832,
            "Roving Gaffer",
            {movie: [("Lighting", "Gaffer")] for movie in movies},
        )
        # Directed 2 films and acted in 1 of them. The tie reads as Director.
        both = make_crew_person(
            833,
            "Hyphenate Auteur",
            {movie: [("Directing", "Director")] for movie in movies[:2]},
        )
        db.session.add(
            MovieCast(
                movie_id=movies[0].id,
                credit_id=both.id,
                character="The Cameo",
                billing_order=5,
            )
        )
        db.session.commit()

    page = admin_client.get("/people?role=all").get_data(as_text=True)
    assert "Steady Director" in page
    assert "Director &middot; 3 titles" in page
    assert "Hyphenate Auteur" in page
    assert "Director &middot; 2 titles" in page

    # A crew job that is not a key role does not count as a credit.

    assert "Roving Gaffer" not in page


def test_people_role_filter_defaults_to_cast(app, admin_client):
    """Filter the page by credit type.

    The default is Cast. People with only crew credits appear behind the
    Crew and the Cast & crew filters. The film counts follow the selected
    type."""

    with app.app_context():
        movies = [make_movie(f"Role Filter Film {n}", 1960 + n) for n in range(3)]
        make_person(841, "Pure Actor", movies[:2])
        make_crew_person(
            842,
            "Pure Director",
            {movie: [("Directing", "Director")] for movie in movies},
        )
        # Directs each film and acts in 2 of them. The cast view counts
        # only the acting appearances.
        hyphenate = make_crew_person(
            843,
            "Sometimes Actor",
            {movie: [("Directing", "Director")] for movie in movies},
        )
        for order, movie in enumerate(movies[:2]):
            db.session.add(
                MovieCast(
                    movie_id=movie.id,
                    credit_id=hyphenate.id,
                    character="Cameo",
                    billing_order=order,
                )
            )
        db.session.commit()

    default_page = admin_client.get("/people").get_data(as_text=True)
    assert "Pure Actor" in default_page
    assert "Pure Director" not in default_page
    assert "Sometimes Actor" in default_page
    assert "Each actor with credits" in default_page
    assert 'id="people-role-cast" value="cast" checked' in default_page

    # Under Cast, the count of the hyphenate is their 2 acting credits,
    # not their 3 directing credits.

    assert default_page.count("2 titles") >= 2
    assert "3 titles" not in default_page

    crew_page = admin_client.get("/people?role=crew").get_data(as_text=True)
    assert "Pure Director" in crew_page
    assert "Sometimes Actor" in crew_page
    assert "3 titles" in crew_page
    assert "Pure Actor" not in crew_page
    assert "Each key crew member with credits" in crew_page

    all_page = admin_client.get("/people?role=all").get_data(as_text=True)
    assert "Pure Actor" in all_page
    assert "Pure Director" in all_page
    assert "Each person with credits" in all_page


def test_search_finds_crew_people_with_roles(app, admin_client):
    """Match crew people in the global search.

    The order is the film count of the key roles. The badge and the
    type-ahead detail show the dominant role."""

    with app.app_context():
        movies = [make_movie(f"Lens Film {n}", 1980 + n) for n in range(2)]
        make_crew_person(
            841,
            "Lens Wizard",
            {
                movies[0]: [("Camera", "Director of Photography")],
                movies[1]: [("Camera", "Cinematography")],
            },
        )
        db.session.commit()

    page = admin_client.get("/search?q=lens+wizard").get_data(as_text=True)
    assert "Lens Wizard" in page
    assert "Cinematographer &middot; 2 titles" in page

    payload = admin_client.get("/search.json?q=lens wi").get_json()
    people = [r for r in payload["results"] if r["type"] == "Person"]
    assert people and people[0]["detail"] == "Cinematographer · 2 titles"


def test_people_nav_link_present(admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    assert 'href="/people"' in body


def test_film_count_ties_break_on_last_name(app, admin_client):
    """Sort equal film counts by surname, not by first name.

    The surname is the last token of the name. TMDB has no sort-name
    field. Thus, the token replaces it."""

    with app.app_context():
        movies = [make_movie(f"Tie Film {n}", 1960 + n) for n in range(2)]
        # First-name order puts Alan first. Surname order puts Abbott first.
        make_person(821, "Alan Zed", movies)
        make_person(822, "Zoe Abbott", movies)
        db.session.commit()

    page = admin_client.get("/people").get_data(as_text=True)
    assert page.index("Zoe Abbott") < page.index("Alan Zed")


def test_people_ranking_is_cached_until_a_credit_write(app, admin_client):
    """Read the browse page from a ranking that Redis holds.

    Before 2026-08, the full cast and crew aggregation ran 2 times for
    each visit. A TMDB credit apply deletes the ranking. Thus, the people
    of a newly imported film appear on the next view."""

    from app.models import PEOPLE_RANKING_KEY, invalidate_people_ranking

    with app.app_context():
        build_population(app)

    page = admin_client.get("/people").get_data(as_text=True)
    assert "Repertory Regular" in page
    assert app.redis.get(PEOPLE_RANKING_KEY.format(role="cast"))

    with app.app_context():
        movies = [make_movie(f"Late Film {n}", 1990 + n) for n in range(3)]
        make_person(821, "Late Arrival", movies)
        db.session.commit()

    # Served from the ranking. The new person is not in it yet.
    page = admin_client.get("/people").get_data(as_text=True)
    assert "Late Arrival" not in page

    with app.app_context():
        invalidate_people_ranking()
    page = admin_client.get("/people").get_data(as_text=True)
    assert "Late Arrival" in page
    assert page.index("Late Arrival") < page.index("Repertory Regular")

    # Each role filter has its own ranking.
    assert "Late Arrival" in admin_client.get("/people?role=all").get_data(as_text=True)
    assert not app.redis.get(PEOPLE_RANKING_KEY.format(role="crew"))


def test_tmdb_apply_invalidates_the_people_ranking(app):
    """Clear the cached ranking of each role after a TMDB apply credit write."""

    from app.models import PEOPLE_RANKING_KEY

    with app.app_context():
        movie = make_movie("Apply Film", 1999, tmdb_id=960)
        db.session.commit()
        for role in ("cast", "crew", "all"):
            app.redis.set(PEOPLE_RANKING_KEY.format(role=role), "[]")
        movie.tmdb_movie_apply(
            {
                "credits": {
                    "cast": [
                        {
                            "id": 831,
                            "name": "Applied Actor",
                            "character": "Lead",
                            "order": 0,
                        }
                    ],
                    "crew": [],
                }
            }
        )
        db.session.commit()

    for role in ("cast", "crew", "all"):
        assert app.redis.get(PEOPLE_RANKING_KEY.format(role=role)) is None


def test_people_ranking_pages_past_the_default_cap(app, admin_client):
    """Keep the page size of 120 people in the in-memory pagination.

    The base class of Flask-SQLAlchemy limits per_page to 100 unless the
    caller changes the limit."""

    with app.app_context():
        movies = [make_movie("Cap Film One", 1980), make_movie("Cap Film Two", 1981)]
        for n in range(125):
            make_person(9000 + n, f"Cap Person {n:03d}", movies)
        db.session.commit()

    first = admin_client.get("/people").get_data(as_text=True)
    second = admin_client.get("/people?page=2").get_data(as_text=True)
    assert "Cap Person 119" in first and "Cap Person 120" not in first
    assert "Cap Person 120" in second and "Cap Person 124" in second
    assert "Cap Person 119" not in second
