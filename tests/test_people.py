"""The all-people page: everyone credited across the library's films —
cast plus key crew roles — defaulting to multi-film people, with
uncredited-only roles never counting (Glenn's spec from GitHub #13).
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
    """A TMDBCredit with crew rows: {movie: [(department, job), ...]}."""

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
    assert "2 films" in page

    # Single-film people are the long tail; they only appear via search
    assert "One Scene Wonder" not in page

    # Uncredited-only roles never count toward the filter
    assert "Always Uncredited" not in page

    # A missing character string isn't the same as uncredited
    assert "No Character Listed" in page


def test_people_search_widens_to_single_film_people(app, admin_client):
    with app.app_context():
        build_population(app)

    page = admin_client.get("/people?q=Scene+Wonder").get_data(as_text=True)
    assert "One Scene Wonder" in page
    assert "1 film<" in page or "1 film\n" in page or "1 film</span>" in page
    assert "Repertory Regular" not in page


def test_people_ordered_by_film_count(app, admin_client):
    with app.app_context():
        movies = [make_movie(f"Count Film {n}", 1980 + n) for n in range(4)]
        make_person(811, "Busy Actor", movies)
        make_person(812, "Occasional Actor", movies[:2])
        db.session.commit()

    page = admin_client.get("/people").get_data(as_text=True)
    assert page.index("Busy Actor") < page.index("Occasional Actor")
    assert "4 films" in page


def test_people_counts_key_crew_roles_with_role_badges(app, admin_client):
    """Key crew roles join the film-count ordering with a dominant-role
    label; non-key jobs never count (Glenn: grips and gaffers must not
    outrank directors)."""

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
        # Directed two, acted in one of them — the tie reads as Director
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

    page = admin_client.get("/people").get_data(as_text=True)
    assert "Steady Director" in page
    assert "Director &middot; 3 films" in page
    assert "Hyphenate Auteur" in page
    assert "Director &middot; 2 films" in page

    # Non-key crew jobs don't count as credits at all

    assert "Roving Gaffer" not in page


def test_search_finds_crew_people_with_roles(app, admin_client):
    """The global search matches crew people, ordered by key-role film
    count, with the dominant role in the badge and type-ahead detail."""

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
    assert "Cinematographer &middot; 2 films" in page

    payload = admin_client.get("/search.json?q=lens wi").get_json()
    people = [r for r in payload["results"] if r["type"] == "Person"]
    assert people and people[0]["detail"] == "Cinematographer · 2 films"


def test_people_nav_link_present(admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    assert 'href="/people"' in body


def test_film_count_ties_break_on_last_name(app, admin_client):
    """Equal film counts sort by surname (the name's last token), not by
    first name — TMDb has no sort-name field, so the token stands in."""

    with app.app_context():
        movies = [make_movie(f"Tie Film {n}", 1960 + n) for n in range(2)]
        # First-name order would put Alan first; surname order puts Abbott first
        make_person(821, "Alan Zed", movies)
        make_person(822, "Zoe Abbott", movies)
        db.session.commit()

    page = admin_client.get("/people").get_data(as_text=True)
    assert page.index("Zoe Abbott") < page.index("Alan Zed")
