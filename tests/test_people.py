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
    assert "2 titles" in page

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

    page = admin_client.get("/people?role=all").get_data(as_text=True)
    assert "Steady Director" in page
    assert "Director &middot; 3 titles" in page
    assert "Hyphenate Auteur" in page
    assert "Director &middot; 2 titles" in page

    # Non-key crew jobs don't count as credits at all

    assert "Roving Gaffer" not in page


def test_people_role_filter_defaults_to_cast(app, admin_client):
    """The page filters by credit type — Cast by default (crew-only
    people wait behind the Crew and Cast & crew filters), with film
    counts following the selected type."""

    with app.app_context():
        movies = [make_movie(f"Role Filter Film {n}", 1960 + n) for n in range(3)]
        make_person(841, "Pure Actor", movies[:2])
        make_crew_person(
            842,
            "Pure Director",
            {movie: [("Directing", "Director")] for movie in movies},
        )
        # Directs everything, acts twice: the cast view counts only the
        # acting appearances
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
    assert "Every actor credited" in default_page
    assert 'id="people-role-cast" value="cast" checked' in default_page

    # The hyphenate's count under Cast is their two acting credits,
    # not their three directing ones

    assert default_page.count("2 titles") >= 2
    assert "3 titles" not in default_page

    crew_page = admin_client.get("/people?role=crew").get_data(as_text=True)
    assert "Pure Director" in crew_page
    assert "Sometimes Actor" in crew_page
    assert "3 titles" in crew_page
    assert "Pure Actor" not in crew_page
    assert "Every key crew member credited" in crew_page

    all_page = admin_client.get("/people?role=all").get_data(as_text=True)
    assert "Pure Actor" in all_page
    assert "Pure Director" in all_page
    assert "Every person credited" in all_page


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
    assert "Cinematographer &middot; 2 titles" in page

    payload = admin_client.get("/search.json?q=lens wi").get_json()
    people = [r for r in payload["results"] if r["type"] == "Person"]
    assert people and people[0]["detail"] == "Cinematographer · 2 titles"


def test_people_nav_link_present(admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    assert 'href="/people"' in body


def test_film_count_ties_break_on_last_name(app, admin_client):
    """Equal film counts sort by surname (the name's last token), not by
    first name — TMDB has no sort-name field, so the token stands in."""

    with app.app_context():
        movies = [make_movie(f"Tie Film {n}", 1960 + n) for n in range(2)]
        # First-name order would put Alan first; surname order puts Abbott first
        make_person(821, "Alan Zed", movies)
        make_person(822, "Zoe Abbott", movies)
        db.session.commit()

    page = admin_client.get("/people").get_data(as_text=True)
    assert page.index("Zoe Abbott") < page.index("Alan Zed")


def test_people_ranking_is_cached_until_a_credit_write(app, admin_client):
    """The browse page reads a Redis-held ranking — the full cast and
    crew aggregation ran twice a visit before Aug 2026 — and a TMDB
    credit apply drops it, so a newly imported film's people surface
    on the next view."""

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

    # Served from the ranking: the new person isn't in it yet
    page = admin_client.get("/people").get_data(as_text=True)
    assert "Late Arrival" not in page

    with app.app_context():
        invalidate_people_ranking()
    page = admin_client.get("/people").get_data(as_text=True)
    assert "Late Arrival" in page
    assert page.index("Late Arrival") < page.index("Repertory Regular")

    # The role filters rank separately
    assert "Late Arrival" in admin_client.get("/people?role=all").get_data(as_text=True)
    assert not app.redis.get(PEOPLE_RANKING_KEY.format(role="crew"))


def test_tmdb_apply_invalidates_the_people_ranking(app):
    """A credit write through the TMDB apply path clears the cached
    rankings for every role."""

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
    """The in-memory pagination keeps the page's 120-person size —
    Flask-SQLAlchemy's base class caps per_page at 100 unless told
    otherwise."""

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
