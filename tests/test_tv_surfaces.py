"""Test the TV surfaces.

This covers the episode guide of the season page, the episode search,
and the TV credits of the people page. It also covers the Television
section of the filmography and the meta line of the tv page. The series
popover card that the TV Library posters and the filmography posters
show completes the set."""

import json

from app import db
from app.models import TMDBCredit, TMDBGenre, TVCast, TVSeries

from tests.factories import make_tv_file, make_tv_series


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

        # The day-cached payloads replace TMDB. The route reads the cache
        # before it touches the network.
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
    """Give the facts of each Television tile to the series popover.

    This works the same as the film tiles. Each poster arms a /tv_card
    fetch. An owned series uses the series id. An unowned series uses
    the TMDB id. The In-library badge is not under the poster now,
    because the card shows it.
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

    # There is no badge under the poster now. The card shows it.

    assert "In library" not in page

    # An owned series arms the card by series id. An unowned series uses the TMDB id.

    with app.app_context():
        lagging_id = TVSeries.query.filter_by(tmdb_id=2041).one().id
    assert f"/tv_card?series_id={lagging_id}" in page
    assert "/tv_card?tmdb_id=2043" in page


def test_tv_card_carries_the_series_facts(app, admin_client):
    """Show the series popover in the same shape as the film card.

    It shows the title. It shows the meta line of the run in place of
    "Directed by … · 96 min". It shows the synopsis, the billed cast,
    the In-library badge in its shopping colors, and the part of the
    run that is on the shelf."""

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
            tmdb_content_rating="TV-PG",
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

    # The script in base.html looks for the shared popover class.

    assert 'class="poster-card"' in page
    assert "The Prisoner" in page
    assert f"/tv/{series_id}" in page
    assert "1967–1968 · 1 season, 17 episodes · Mystery" in page
    assert ">TV-PG</span>" in page
    assert "A resigning agent wakes" in page
    assert "Patrick McGoohan" in page
    assert "/library/movie?credit=9102" in page

    # 2 Blu-ray episodes. They are settled, and the card counts them.

    assert 'text-bg-success align-middle me-1" title="In your Fitzflix library' in page
    assert "1 season, 2 episodes in your library" in page

    # The card does not show facts that apply only to films.

    assert "On your watchlist" not in page
    assert "Play on Apple TV" not in page
    assert "JustWatch" not in page


def test_tv_card_badge_tracks_the_seasons(app, admin_client):
    """Color the In-library badge of the card the same as each other badge (#191).

    The badge is amber while a season has an episode that is worth an
    upgrade. The badge is green when the whole run is settled. A DVD is
    as good as that release can get."""

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
    """Give a card to an unowned television credit of a person.

    The key is the TMDB id, with no local row. The card renders from
    TMDB and shows no badge. This is the same as the tmdb lane of the
    film card."""

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
                "content_ratings": {
                    "results": [
                        {"iso_3166_1": "DE", "rating": "12"},
                        {"iso_3166_1": "US", "rating": "TV-MA"},
                    ]
                },
            }

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(discover, "tmdb_get", lambda *a, **k: FakeResponse())

    page = admin_client.get("/tv_card?tmdb_id=999123").get_data(as_text=True)
    assert 'class="poster-card"' in page
    assert "Nowhere Show" in page
    assert "1980–1984 · 4 seasons, 52 episodes · Comedy" in page
    assert ">TV-MA</span>" in page
    assert ">12</span>" not in page
    assert "Someone Famous" in page

    # Nothing is owned. Thus, there is no badge and no shelf count. There
    # is no local record. Thus, there is no series page to link from the
    # title.

    assert "In library" not in page
    assert "in your library" not in page
    assert "/tv/" not in page


def test_tv_library_posters_open_the_series_popover(app, admin_client):
    """Show the same card on the posters of the TV Library.

    Only the poster is armed. A tap on the season list beside it still
    navigates."""

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

    # The block of the series has exactly 1 armed element: the anchor
    # around the poster. The season rows beside it stay plain links.

    assert block.count("data-card-url") == 1
    assert f"/tv_card?series_id={series_id}" in block
    armed = block[block.index("data-card-url") :]
    assert armed[: armed.index("</a>")].count("/popover.jpg") == 1
    assert f"/tv/{series_id}/1" in block


def test_series_lists_title_by_tmdb_name_and_year(app, admin_client):
    """Name a series in the TV Library and the TV shopping list by its TMDB name.

    This is the same as each other surface. The folder title that the
    files were imported under is not used. The first-air year goes with
    the name. The library holds 3 series called "Doctor Who", and the
    year tells them apart. A series that TMDB does not know keeps its
    folder title. That title already carries its own disambiguation.
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

        # The heading does not show the folder title. The folder title
        # remains only where the page has no TMDB name to show.

        assert "Avatar - The Last Airbender" not in page, path
        assert "Home Movies Reel (1987)" in page, path


def test_file_activity_card_names_the_series_from_tmdb(app, admin_client):
    """Show the TMDB name in the series link of the File Activity card.

    The link asked for a field that TV rows never had. Thus, it always
    showed the folder title."""

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
    """Answer a stale card url with 404, not with a blank popover.

    Thus, the fetch of the script fails, and no card shows."""

    assert admin_client.get("/tv_card").status_code == 404
    assert admin_client.get("/tv_card?series_id=987654").status_code == 404


def test_self_appearances_drop_but_selfridge_survives(app, admin_client, monkeypatch):
    """Match the self-filter of the Television section at word boundaries.

    The filter drops the talk-show and awards-night rows ("Self", "Self
    - Host", "Herself"). A real character that only contains the letters,
    for example Harry Selfridge, is a real acting credit. It stays."""

    import app.main.library as library

    with app.app_context():
        monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
        db.session.add(TMDBCredit(id=287, name="Jeremy Piven"))
        db.session.commit()

        # The person details and the movie credits read the cache first.
        # Only the tv_credits fetch reaches the patched network call.
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
    """Show the meta line first on the series page, then the synopsis.

    The meta line has the run, the size, the genres, and the US content
    rating in its bordered box. This is the order of the popover card."""

    from datetime import datetime

    with app.app_context():
        series = make_tv_series(
            "Doctor Who (1963)",
            tmdb_id=121,
            tmdb_first_air_date=datetime(1963, 11, 23),
            tmdb_last_air_date=datetime(1989, 12, 6),
            tmdb_number_of_seasons=26,
            tmdb_number_of_episodes=694,
            tmdb_content_rating="TV-PG",
            tmdb_overview="A Time Lord wanders time and space.",
        )
        db.session.commit()
        series_id = series.id

    response = admin_client.get(f"/tv/{series_id}")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "1963–1989" in page
    assert "26 seasons, 694 episodes" in page
    assert ">TV-PG</span>" in page
    assert page.index("26 seasons") < page.index("A Time Lord wanders")


def test_series_upgradable_reads_physical_from_the_files_own_tier(app):
    """Read the verdict from the quality row of the file itself (#238).

    RefQuality.preference has no unique constraint. The old
    implementation resolved the worst preference of a season back to a
    tier with a join on that VALUE. Thus, a non-physical tier with the
    same preference number as the DVD made a DVD-only season read as
    upgradable."""

    from app.main.helpers import series_upgradable
    from app.models import RefQuality

    with app.app_context():
        series = make_tv_series("Tied Tier Show")
        make_tv_file(series, 1, 1, "DVD")
        dvd = RefQuality.query.filter_by(quality_title="DVD").one()
        db.session.add(
            RefQuality(
                quality_title="Tied Tier",
                preference=dvd.preference,
                physical_media=False,
            )
        )
        db.session.commit()

        # A DVD season is as good as that release can get. Thus, it is
        # green. The number of other tiers with its preference number is
        # not important.

        assert series_upgradable([series.id]) == {series.id: False}


def test_a_tied_worst_copy_prefers_the_upgradable_episode(app):
    """Keep the season amber when a non-physical copy ties at the worst (#238).

    2 episodes tie at the worst of the season. 1 is on physical media
    and 1 is not. The non-physical copy CAN be upgraded. Thus, the
    season still has an episode that is worth an upgrade."""

    from app.main.helpers import series_upgradable
    from app.models import RefQuality

    with app.app_context():
        dvd = RefQuality.query.filter_by(quality_title="DVD").one()
        db.session.add(
            RefQuality(
                quality_title="Tied Web Tier",
                preference=dvd.preference,
                physical_media=False,
            )
        )
        db.session.flush()
        series = make_tv_series("Tied Worst Show")
        make_tv_file(series, 1, 1, "DVD")
        make_tv_file(series, 1, 2, "Tied Web Tier")
        db.session.commit()

        assert series_upgradable([series.id]) == {series.id: True}


def test_tv_library_search_matches_series_titles_only(app, admin_client):
    """Test the search of the TV library (#210, revised again).

    The series list holds only the series with a TITLE (or TMDB name)
    that matches. Fitzflix stores no episode metadata. Thus, there is
    no episode section."""

    import re

    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        by_title = make_tv_series("Quincke's Casebook")
        make_tv_file(by_title, 1, 1, "SDTV")
        by_tmdb_name = make_tv_series("Folder Name Only", tmdb_name="Quincke Files")
        make_tv_file(by_tmdb_name, 1, 1, "SDTV")
        unrelated = make_tv_series("Unrelated Show")
        make_tv_file(unrelated, 1, 1, "SDTV")
        # A movie with the same term must never leak into the TV library.
        movie = make_movie("Quincke The Movie", 1999)
        make_movie_file(movie, "DVD")
        db.session.commit()
        title_id = by_title.id

    page = admin_client.get("/library/tv?q=quincke").get_data(as_text=True)
    assert "TV library matches for &#39;quincke&#39;" in page
    assert "Quincke&#39;s Casebook" in page
    assert "Quincke Files" in page
    assert "Unrelated Show" not in page
    assert "Quincke The Movie" not in page
    assert "2 series titles match" in page
    assert f'href="/tv/{title_id}"' in page

    # The search box posts and then redirects to ?q=

    plain = admin_client.get("/library/tv").get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', plain).group(1)
    response = admin_client.post(
        "/library/tv",
        data={
            "csrf_token": token,
            "search_query": "quincke",
            "search_submit": "Search",
        },
    )
    assert response.status_code == 302
    assert "q=quincke" in response.headers["Location"]

    # When there are no matches, the page offers the whole library.

    page = admin_client.get("/library/tv?q=zzzzzz").get_data(as_text=True)
    assert "No TV series match" in page
    assert "Show the whole TV library" in page
