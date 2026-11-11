"""Test the all-people page.

The page lists each person with a credit in the films of the library.
This includes the cast and the key crew roles. By default, it shows
only the people with more than 1 film. A role that is only uncredited
never counts. This is the specification from Glenn in GitHub #13.
"""

from app import db
from app.models import Movie, MovieCast, MovieCrew, TMDBCredit
from tests.factories import make_movie
from tests.factories import make_movie_file
from tests.factories import make_person


def make_owned_movie(title, year):
    """Make a movie with a main feature file. Only the owned works count (#656)."""

    movie = make_movie(title, year)
    make_movie_file(movie, "Bluray-1080p")
    return movie


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
    first = make_owned_movie("People Film One", 1970)
    second = make_owned_movie("People Film Two", 1971)
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


def test_people_page_text_matches_the_counted_records(app, admin_client):
    """Test that the page text says what the counts hold (#331, #656).

    The counts hold only the films and the series that have a file.
    The text says so. The #331 wording about watchlist and diary
    records is gone with the records from the counts."""

    with app.app_context():
        build_population(app)

    page = admin_client.get("/people").get_data(as_text=True)
    assert "in more than one film or series in the library" in page
    assert "The counts hold only the films and the series that have a file." in page
    assert "that Fitzflix has a record of" not in page
    assert "2 titles" in page


def test_file_less_records_do_not_count(app, admin_client):
    """Count only the owned works on the People page and in the search (#656).

    A watchlist, a diary, a catalog, or the TMDB review page makes a
    movie record without a file. The user cannot see which records
    exist. A badge that counts them reads as wrong against the "In
    library" filter of the filmography page. On 2026-11-03 Margaret
    Qualley said "3 titles" in the search and showed 1 owned film."""

    with app.app_context():
        owned = [make_owned_movie(f"Owned Film {n}", 2000 + n) for n in range(2)]
        listed = make_movie("Watchlist Only Film", 2010)
        make_person(861, "Qualley Example", owned + [listed])
        # An extra of an owned film is not a main feature. It does
        # not make a second owned work.
        make_movie_file(owned[0], "Bluray-1080p", "Featurettes")
        # A person with only file-less records does not list at all.
        make_person(862, "Record Only Person", [listed, make_movie("Diary Film", 2011)])
        db.session.commit()

    page = admin_client.get("/people?q=Qualley").get_data(as_text=True)
    assert "Qualley Example" in page
    assert "2 titles in library" in page
    assert "3 titles" not in page

    # The badge names what it counts (Glenn, 2026-11-03).

    page = admin_client.get("/search?q=qualley").get_data(as_text=True)
    assert "Actor &middot; 2 titles in library</span>" in page

    page = admin_client.get("/people?q=Record+Only").get_data(as_text=True)
    assert "Record Only Person" not in page


def test_the_role_counts_only_the_owned_works(app, admin_client):
    """The role and the count of a person come from the same owned works (#692).

    The person acts in 2 owned films and directs 3 records with no file.
    The directing credits do not make the person a director."""

    with app.app_context():
        owned = [make_owned_movie(f"Owned Role Film {n}", 2020 + n) for n in range(2)]
        unowned = [make_movie(f"Listed Role Film {n}", 2023 + n) for n in range(3)]
        make_person(871, "Owned Role Example", owned)
        db.session.add_all(
            MovieCrew(
                movie_id=movie.id,
                credit_id=871,
                department="Directing",
                job="Director",
            )
            for movie in unowned
        )
        db.session.commit()

    page = admin_client.get("/people?role=all&q=Owned+Role").get_data(as_text=True)
    assert "Owned Role Example" in page
    assert "Actor &middot; 2 titles" in page
    assert "Director &middot;" not in page

    page = admin_client.get("/search?q=owned+role+example").get_data(as_text=True)
    assert "Actor &middot; 2 titles in library</span>" in page

    payload = admin_client.get("/search.json?q=owned role ex").get_json()
    people = [r for r in payload["results"] if r["type"] == "Person"]
    assert people and people[0]["detail"] == "Actor · 2 titles in library"


def test_a_file_commit_invalidates_the_people_ranking(app):
    """Clear the cached ranking of each role after a commit that adds or deletes a file (#656).

    The counts hold only the owned works. Thus, an import and a file
    delete move them in the same way as a credit write. A rollback
    leaves the ranking alone."""

    from app.tmdb_apply import PEOPLE_RANKING_KEY

    def prime():
        for role in ("cast", "crew", "all"):
            app.redis.set(PEOPLE_RANKING_KEY.format(role=role), "[]")

    def cached():
        return [
            app.redis.get(PEOPLE_RANKING_KEY.format(role=role))
            for role in ("cast", "crew", "all")
        ]

    with app.app_context():
        movie = make_movie("Fresh Import", 2001)
        db.session.commit()
        prime()
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.rollback()
        assert all(cached())

        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        assert cached() == [None, None, None]

        prime()
        movie.title = "Fresh Import Renamed"
        db.session.commit()
        assert all(cached())

        db.session.delete(file)
        db.session.commit()
        assert cached() == [None, None, None]


def test_people_search_widens_to_single_film_people(app, admin_client):
    with app.app_context():
        build_population(app)

    page = admin_client.get("/people?q=Scene+Wonder").get_data(as_text=True)
    assert "One Scene Wonder" in page
    assert "1 title in library</span>" in page
    assert "Repertory Regular" not in page


def test_people_ordered_by_film_count(app, admin_client):
    with app.app_context():
        movies = [make_owned_movie(f"Count Film {n}", 1980 + n) for n in range(4)]
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
        movies = [make_owned_movie(f"Crew Film {n}", 1970 + n) for n in range(3)]
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
        movies = [make_owned_movie(f"Role Filter Film {n}", 1960 + n) for n in range(3)]
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
        movies = [make_owned_movie(f"Lens Film {n}", 1980 + n) for n in range(2)]
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
    assert people and people[0]["detail"] == "Cinematographer · 2 titles in library"


def test_people_nav_link_present(admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    assert 'href="/people"' in body


def test_film_count_ties_break_on_last_name(app, admin_client):
    """Sort equal film counts by surname, not by first name.

    The surname is the last token of the name. TMDB has no sort-name
    field. Thus, the token replaces it."""

    with app.app_context():
        movies = [make_owned_movie(f"Tie Film {n}", 1960 + n) for n in range(2)]
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

    from app.tmdb_apply import PEOPLE_RANKING_KEY, invalidate_people_ranking

    with app.app_context():
        build_population(app)

    page = admin_client.get("/people").get_data(as_text=True)
    assert "Repertory Regular" in page
    assert app.redis.get(PEOPLE_RANKING_KEY.format(role="cast"))

    # The films commit before the ranking is read. A file commit drops
    # the ranking on its own (#656). This test covers the credit write.

    with app.app_context():
        movies = [make_owned_movie(f"Late Film {n}", 1990 + n) for n in range(3)]
        db.session.commit()
    page = admin_client.get("/people").get_data(as_text=True)
    assert app.redis.get(PEOPLE_RANKING_KEY.format(role="cast"))

    with app.app_context():
        movies = Movie.query.filter(Movie.title.like("Late Film %")).all()
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

    from app.tmdb_apply import PEOPLE_RANKING_KEY

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
        movies = [
            make_owned_movie("Cap Film One", 1980),
            make_owned_movie("Cap Film Two", 1981),
        ]
        for n in range(125):
            make_person(9000 + n, f"Cap Person {n:03d}", movies)
        db.session.commit()

    first = admin_client.get("/people").get_data(as_text=True)
    second = admin_client.get("/people?page=2").get_data(as_text=True)
    assert "Cap Person 119" in first and "Cap Person 120" not in first
    assert "Cap Person 120" in second and "Cap Person 124" in second
    assert "Cap Person 119" not in second
