"""TV surfaces: the season-page episode guide, episode search,
people-page TV credits, the filmography Television section, the
tv-page meta line, and the series popover card the TV Library and
filmography posters show."""

import json

from app import db
from app.models import TMDBCredit, TMDBGenre, TVCast, TVSeries
from app.tv_validation import VALIDATION_KEY

from tests.factories import make_tv_episode, make_tv_file, make_tv_series


def _suspect_verdict(series_id):
    return json.dumps(
        {
            "name": "whatever",
            "compared": 10,
            "agreed": 0,
            "rate": 0.0,
            "suspect": True,
            "examples": [],
        }
    )


def test_season_page_shows_guide_and_title_column(app, admin_client):
    with app.app_context():
        series = make_tv_series("Doctor Who (1963)", tmdb_id=121)
        make_tv_episode(
            series,
            1,
            1,
            title="An Unearthly Child",
            overview="Two teachers follow a strange pupil home.",
        )
        make_tv_episode(series, 1, 2, title="The Cave of Skulls")
        make_tv_file(series, 1, 1, "DVD")
        db.session.commit()
        series_id = series.id

    response = admin_client.get(f"/tv/{series_id}/1")
    assert response.status_code == 200
    assert b"Episodes" in response.data
    assert b"An Unearthly Child" in response.data
    assert b"The Cave of Skulls" in response.data
    assert b"In library" in response.data


def test_season_page_episode_badges_wear_shopping_colors(app, admin_client):
    """The guide's In-library badge is amber or green like every other
    one (#191): the WEBDL episode is worth upgrading, while the DVD is
    the only release that will ever exist, so it reads settled."""

    with app.app_context():
        series = make_tv_series("Quality Show", tmdb_id=1212)
        make_tv_episode(series, 1, 1, title="The Lagging One")
        make_tv_episode(series, 1, 2, title="The Settled One")
        make_tv_file(series, 1, 1, "WEBDL-720p")
        make_tv_file(series, 1, 2, "DVD")
        db.session.commit()
        series_id = series.id

    page = admin_client.get(f"/tv/{series_id}/1").get_data(as_text=True)
    lagging = page[page.index("The Lagging One") :]
    settled = page[page.index("The Settled One") :]
    assert "text-bg-warning" in lagging[: lagging.index("In library")]
    assert "text-bg-success" in settled[: settled.index("In library")]


def test_season_page_suspect_series_stays_plain(app, admin_client):
    with app.app_context():
        series = make_tv_series("Cursed Show", tmdb_id=999)
        make_tv_episode(series, 1, 1, title="Wrong Title")
        make_tv_file(series, 1, 1, "DVD")
        db.session.commit()
        series_id = series.id
        app.redis.hset(VALIDATION_KEY, str(series_id), _suspect_verdict(series_id))

    response = admin_client.get(f"/tv/{series_id}/1")
    assert response.status_code == 200
    assert b"Wrong Title" not in response.data


def test_search_finds_episode_titles_but_not_suspect_ones(app, admin_client):
    with app.app_context():
        good = make_tv_series("Columbo", tmdb_id=1041)
        make_tv_episode(good, 1, 1, title="Murder by the Book")
        cursed = make_tv_series("Cursed Show", tmdb_id=999)
        make_tv_episode(cursed, 1, 1, title="Murder by the Wrong Book")
        db.session.commit()
        good_id = good.id
        app.redis.hset(VALIDATION_KEY, str(cursed.id), _suspect_verdict(cursed.id))

    response = admin_client.get("/search?q=Murder+by+the")
    assert response.status_code == 200
    assert b"Murder by the Book" in response.data
    assert f"/tv/{good_id}/1".encode() in response.data
    assert b"Murder by the Wrong Book" not in response.data


def test_people_page_counts_tv_credits(app, admin_client):
    with app.app_context():
        falk = TMDBCredit(id=4886, name="Peter Falk")
        db.session.add(falk)
        columbo = make_tv_series("Columbo", tmdb_id=1041)
        rockford = make_tv_series("The Rockford Files", tmdb_id=1042)
        db.session.add(
            TVCast(tv_id=columbo.id, credit_id=4886, character="Lt. Columbo")
        )
        db.session.add(TVCast(tv_id=rockford.id, credit_id=4886, character="A Cop"))
        db.session.commit()

    response = admin_client.get("/people?q=Falk")
    assert response.status_code == 200
    assert b"Peter Falk" in response.data
    assert b"2 titles" in response.data


def test_filmography_shows_television_section(app, admin_client, monkeypatch):
    with app.app_context():
        monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
        series = make_tv_series("Columbo", tmdb_id=1041)
        make_tv_file(series, 1, 1, "DVD")
        db.session.add(TMDBCredit(id=4886, name="Peter Falk"))
        db.session.commit()
        series_id = series.id

        # Day-cached payloads stand in for TMDb: the route reads the
        # cache before ever touching the network
        app.redis.set(
            "fitzflix:tmdb:person:4886:details", json.dumps({"name": "Peter Falk"})
        )
        app.redis.set(
            "fitzflix:tmdb:person:4886:credits",
            json.dumps({"cast": [], "crew": []}),
        )
        app.redis.set(
            "fitzflix:tmdb:person:4886:tv_credits",
            json.dumps(
                {
                    "cast": [
                        {
                            "id": 1041,
                            "name": "Columbo",
                            "first_air_date": "1971-09-15",
                            "character": "Lt. Columbo",
                            "episode_count": 69,
                            "poster_path": None,
                        }
                    ],
                    "crew": [],
                }
            ),
        )

    response = admin_client.get("/library/movie?credit=4886")
    assert response.status_code == 200
    assert b"Television" in response.data
    assert b"Columbo" in response.data
    assert f"/tv/{series_id}".encode() in response.data
    assert b"Lt. Columbo" in response.data


def test_filmography_television_tiles_open_the_series_popover(
    app, admin_client, monkeypatch
):
    """The Television tiles hand their facts to the series popover the
    way the film tiles do: each poster arms a /tv_card fetch — by
    series id when owned, by TMDb id when not — and the In-library
    badge no longer sits under the poster, since the card carries it.
    """

    with app.app_context():
        monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
        lagging = make_tv_series("Lagging Show", tmdb_id=2041)
        make_tv_file(lagging, 1, 1, "WEBDL-720p")
        settled = make_tv_series("Settled Show", tmdb_id=2042)
        make_tv_file(settled, 1, 1, "DVD")
        db.session.add(TMDBCredit(id=5001, name="Jane Player"))
        db.session.commit()

        app.redis.set(
            "fitzflix:tmdb:person:5001:details", json.dumps({"name": "Jane Player"})
        )
        app.redis.set(
            "fitzflix:tmdb:person:5001:credits",
            json.dumps({"cast": [], "crew": []}),
        )
        app.redis.set(
            "fitzflix:tmdb:person:5001:tv_credits",
            json.dumps(
                {
                    "cast": [
                        {
                            "id": 2041,
                            "name": "Lagging Show",
                            "first_air_date": "1971-01-01",
                            "character": "The Lead",
                            "poster_path": None,
                        },
                        {
                            "id": 2042,
                            "name": "Settled Show",
                            "first_air_date": "1972-01-01",
                            "character": "The Other One",
                            "poster_path": None,
                        },
                        {
                            "id": 2043,
                            "name": "Unowned Show",
                            "first_air_date": "1973-01-01",
                            "character": "A Guest",
                            "poster_path": None,
                        },
                    ],
                    "crew": [],
                }
            ),
        )

    page = admin_client.get("/library/movie?credit=5001").get_data(as_text=True)

    # Nothing badges under the poster any more — the card says it

    assert "In library" not in page

    # Owned series arm the card by series id, unowned ones by TMDb id

    with app.app_context():
        lagging_id = TVSeries.query.filter_by(tmdb_id=2041).one().id
    assert f"/tv_card?series_id={lagging_id}" in page
    assert "/tv_card?tmdb_id=2043" in page


def test_tv_card_carries_the_series_facts(app, admin_client):
    """The series popover is the film card's shape in TV terms: title,
    the run's meta line standing in for "Directed by … · 96 min", the
    synopsis, the billed cast, the In-library badge in its shopping
    colors, and how much of the run is actually on the shelf."""

    from datetime import datetime

    with app.app_context():
        series = make_tv_series(
            "The Prisoner (1967)",
            tmdb_id=3391,
            tmdb_name="The Prisoner",
            tmdb_overview="A resigning agent wakes in a village he cannot leave.",
            tmdb_first_air_date=datetime(1967, 9, 29),
            tmdb_last_air_date=datetime(1968, 2, 1),
            tmdb_number_of_seasons=1,
            tmdb_number_of_episodes=17,
        )
        genre = TMDBGenre(id=9101, name="Mystery")
        db.session.add(genre)
        series.genres.append(genre)
        credit = TMDBCredit(id=9102, name="Patrick McGoohan")
        db.session.add(credit)
        db.session.flush()
        db.session.add(
            TVCast(
                tv_id=series.id,
                credit_id=credit.id,
                character="Number Six",
                billing_order=0,
                episode_count=17,
            )
        )
        make_tv_file(series, 1, 1, "Bluray-1080p")
        make_tv_file(series, 1, 2, "Bluray-1080p")
        db.session.commit()
        series_id = series.id

    page = admin_client.get(f"/tv_card?series_id={series_id}").get_data(as_text=True)

    # The shared popover class is what base.html's script looks for

    assert 'class="poster-card"' in page
    assert "The Prisoner" in page
    assert f"/tv/{series_id}" in page
    assert "1967–1968 · 1 season, 17 episodes · Mystery" in page
    assert "A resigning agent wakes" in page
    assert "Patrick McGoohan" in page
    assert "/library/movie?credit=9102" in page

    # Two Blu-ray episodes: settled, and counted

    assert 'text-bg-success align-middle me-1" title="In your Fitzflix library' in page
    assert "1 season, 2 episodes in your library" in page

    # Film-only facts stay off the card

    assert "On your watchlist" not in page
    assert "Play on Apple TV" not in page
    assert "JustWatch" not in page


def test_tv_card_badge_tracks_the_seasons(app, admin_client):
    """The card's In-library badge answers the way every other one does
    (#191): amber while a season still has an episode worth upgrading,
    green once the whole run is settled — a DVD being as good as that
    release will ever get."""

    with app.app_context():
        lagging = make_tv_series("Lagging Card Show", tmdb_id=3401)
        make_tv_file(lagging, 1, 1, "WEBDL-720p")
        settled = make_tv_series("Settled Card Show", tmdb_id=3402)
        make_tv_file(settled, 1, 1, "DVD")
        db.session.commit()
        lagging_id, settled_id = lagging.id, settled.id

    page = admin_client.get(f"/tv_card?series_id={lagging_id}").get_data(as_text=True)
    assert 'text-bg-warning align-middle me-1" title="In your Fitzflix library' in page
    page = admin_client.get(f"/tv_card?series_id={settled_id}").get_data(as_text=True)
    assert 'text-bg-success align-middle me-1" title="In your Fitzflix library' in page


def test_tv_card_renders_an_unowned_series_from_tmdb(app, admin_client, monkeypatch):
    """A person's unowned television credit still gets a card: keyed by
    TMDb id with no local row, it renders from TMDb and badges nothing,
    the way the film card's tmdb lane does."""

    import app.main.discover as discover

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "name": "Nowhere Show",
                "first_air_date": "1980-01-01",
                "last_air_date": "1984-01-01",
                "number_of_seasons": 4,
                "number_of_episodes": 52,
                "overview": "A show the library has never held.",
                "genres": [{"name": "Comedy"}],
                "aggregate_credits": {
                    "cast": [{"id": 9200, "name": "Someone Famous", "order": 0}]
                },
            }

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(discover, "tmdb_get", lambda *a, **k: FakeResponse())

    page = admin_client.get("/tv_card?tmdb_id=999123").get_data(as_text=True)
    assert 'class="poster-card"' in page
    assert "Nowhere Show" in page
    assert "1980–1984 · 4 seasons, 52 episodes · Comedy" in page
    assert "Someone Famous" in page

    # Nothing owned, so no badge and no shelf count — and with no local
    # record there's no series page to link the title to

    assert "In library" not in page
    assert "in your library" not in page
    assert "/tv/" not in page


def test_tv_library_posters_open_the_series_popover(app, admin_client):
    """The TV Library's posters carry the same card. Only the poster is
    armed — the season list beside it still navigates on a tap."""

    with app.app_context():
        series = make_tv_series(
            "Poster Popover Show", tmdb_id=3501, tmdb_poster_path="/popover.jpg"
        )
        make_tv_file(series, 1, 1, "DVD")
        db.session.commit()
        series_id = series.id

    page = admin_client.get("/library/tv").get_data(as_text=True)
    block = page[page.index(f'id="{series_id}" style="scroll-margin-top') :]
    block = block[: block.index("<hr>")]

    # Exactly one armed element in the series' block: the anchor around
    # the poster. The season rows beside it stay plain links

    assert block.count("data-card-url") == 1
    assert f"/tv_card?series_id={series_id}" in block
    armed = block[block.index("data-card-url") :]
    assert armed[: armed.index("</a>")].count("/popover.jpg") == 1
    assert f"/tv/{series_id}/1" in block


def test_series_lists_title_by_tmdb_name_and_year(app, admin_client):
    """The TV Library and TV shopping list name a series the way every
    other surface does — its TMDb name, not the folder title the files
    were imported under. The first-air year rides along because the
    library holds three series called "Doctor Who" and the year is what
    tells them apart. A series TMDb doesn't know keeps its folder
    title, which already carries whatever disambiguation it has.
    """

    from datetime import datetime

    with app.app_context():
        named = make_tv_series(
            "Avatar - The Last Airbender",
            tmdb_id=4278,
            tmdb_name="Avatar: The Last Airbender",
            tmdb_first_air_date=datetime(2005, 2, 21),
        )
        make_tv_file(named, 1, 1, "DVD")
        unmatched = make_tv_series("Home Movies Reel (1987)")
        make_tv_file(unmatched, 1, 1, "DVD")
        db.session.commit()

    for path in ("/library/tv", "/shopping-list/tv"):
        page = admin_client.get(path).get_data(as_text=True)
        assert "Avatar: The Last Airbender (2005)" in page, path

        # The folder title is gone from the heading — it survives only
        # where the page has no TMDb name to show

        assert "Avatar - The Last Airbender" not in page, path
        assert "Home Movies Reel (1987)" in page, path


def test_file_activity_card_names_the_series_from_tmdb(app, admin_client):
    """The File Activity card's series link reads the TMDb name too —
    it asked for a field TV rows have never had, so it always showed
    the folder title."""

    with app.app_context():
        series = make_tv_series("Mash", tmdb_id=918, tmdb_name="M*A*S*H")
        file = make_tv_file(series, 1, 1, "DVD")
        db.session.commit()
        basename = file.basename

    page = admin_client.get(f"/file-activity/card?basename={basename}").get_data(
        as_text=True
    )
    assert "M*A*S*H" in page


def test_tv_card_404s_for_an_unknown_series(app, admin_client):
    """A stale card url answers 404 rather than a blank popover, so the
    script's fetch simply fails and no card shows."""

    assert admin_client.get("/tv_card").status_code == 404
    assert admin_client.get("/tv_card?series_id=987654").status_code == 404


def test_self_appearances_drop_but_selfridge_survives(app, admin_client, monkeypatch):
    """The Television section's self-filter matches at word boundaries:
    talk-show and awards-night rows ("Self", "Self - Host", "Herself")
    are dropped, but a genuine character that merely contains the
    letters — Harry Selfridge — is a real acting credit and stays."""

    import app.main.library as library

    with app.app_context():
        monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
        db.session.add(TMDBCredit(id=287, name="Jeremy Piven"))
        db.session.commit()

        # Person details and movie credits read cache-first; only the
        # tv_credits fetch reaches the patched network call
        app.redis.set(
            "fitzflix:tmdb:person:287:details", json.dumps({"name": "Jeremy Piven"})
        )
        app.redis.set(
            "fitzflix:tmdb:person:287:credits",
            json.dumps({"cast": [], "crew": []}),
        )

        payload = {
            "cast": [
                {
                    "id": 33217,
                    "name": "Mr Selfridge",
                    "first_air_date": "2013-01-06",
                    "character": "Harry Selfridge",
                    "episode_count": 40,
                    "poster_path": None,
                },
                {
                    "id": 2,
                    "name": "Talk Show",
                    "first_air_date": "2010-01-01",
                    "character": "Self",
                    "episode_count": 3,
                    "poster_path": None,
                },
                {
                    "id": 3,
                    "name": "Award Night",
                    "first_air_date": "2011-01-01",
                    "character": "Self - Host",
                    "episode_count": 1,
                    "poster_path": None,
                },
                {
                    "id": 4,
                    "name": "Retrospective",
                    "first_air_date": "2012-01-01",
                    "character": "Himself (archive footage)",
                    "episode_count": 2,
                    "poster_path": None,
                },
            ],
            "crew": [],
        }

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        monkeypatch.setattr(library, "tmdb_get", lambda *a, **kw: FakeResponse())

    response = admin_client.get("/library/movie?credit=287")
    assert response.status_code == 200
    assert b"Mr Selfridge" in response.data
    assert b"Harry Selfridge" in response.data
    assert b"Talk Show" not in response.data
    assert b"Award Night" not in response.data
    assert b"Retrospective" not in response.data


def test_tv_page_meta_line(app, admin_client):
    from datetime import datetime

    with app.app_context():
        series = make_tv_series(
            "Doctor Who (1963)",
            tmdb_id=121,
            tmdb_first_air_date=datetime(1963, 11, 23),
            tmdb_last_air_date=datetime(1989, 12, 6),
            tmdb_number_of_seasons=26,
            tmdb_number_of_episodes=694,
        )
        db.session.commit()
        series_id = series.id

    response = admin_client.get(f"/tv/{series_id}")
    assert response.status_code == 200
    assert "1963–1989".encode() in response.data
    assert b"26 seasons, 694 episodes" in response.data
