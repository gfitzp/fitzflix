"""Test the Criterion Collection refresh from Wikidata.

The tests cover the SPARQL response parser, the access etiquette headers,
the match order (TMDB id first, then title and year), the protection of
hand-set fields, and the Redis cache.
"""

import json

from app import db
from app.models import Movie
from app.videos import refresh_criterion_collection_info

from tests.factories import make_movie

import app.videos as videos

SPARQL_RESPONSE = {
    "results": {
        "bindings": [
            {
                # Fitzflix matches this row by TMDB id. The library title is
                # different.
                "spine": {"value": "1"},
                "tmdbId": {"value": "1863"},
                "filmLabel": {"value": "La Grande Illusion"},
                "year": {"value": "1937"},
                "criterionId": {"value": "336-grand-illusion"},
            },
            {
                # Wikidata has no TMDB id. Fitzflix matches by title and year.
                "spine": {"value": "2"},
                "filmLabel": {"value": "Seven Samurai"},
                "year": {"value": "1954"},
            },
            {
                # Fitzflix skips a row with a spine that it cannot parse.
                # The row is not a fatal error.
                "spine": {"value": "n/a"},
                "filmLabel": {"value": "Broken Row"},
            },
        ]
    }
}


SETS_RESPONSE = {
    "results": {
        "bindings": [
            {
                # This is a box set. The spine and the title belong to the
                # set item. The film details belong to the member.
                "spine": {"value": "1000"},
                "setLabel": {"value": "Godzilla: The Showa-Era Films, 1954-1975"},
                "tmdbId": {"value": "39462"},
                "filmLabel": {"value": "All Monsters Attack"},
                "year": {"value": "1969"},
                "criterionId": {"value": "29306"},
            },
        ]
    }
}


def fake_sparql(monkeypatch, calls=None):

    import app.criterion_catalog as criterion_catalog

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_get(url, params=None, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "params": params, "headers": headers})
        if "P527" in (params or {}).get("query", ""):
            return FakeResponse(SETS_RESPONSE)
        return FakeResponse(SPARQL_RESPONSE)

    monkeypatch.setattr(criterion_catalog.requests, "get", fake_get)


def test_refresh_matches_by_tmdb_id_and_title_year(app, monkeypatch):
    calls = []
    fake_sparql(monkeypatch, calls)

    with app.app_context():
        # The library title is different from the Wikidata label. Only
        # the TMDB id can connect them.

        by_id = make_movie("Grand Illusion", 1937, tmdb_id=1863)

        # There is no TMDB match. Fitzflix then matches by title and year.

        by_title = make_movie("Seven Samurai", 1954)

        # This is not a Criterion release. Fitzflix does not change it.

        other = make_movie("Sharknado", 2013, tmdb_id=119283)
        db.session.commit()
        ids = (by_id.id, by_title.id, other.id)

        assert refresh_criterion_collection_info() is True

        db.session.expire_all()
        assert db.session.get(Movie, ids[0]).criterion_spine_number == 1
        assert db.session.get(Movie, ids[1]).criterion_spine_number == 2
        assert db.session.get(Movie, ids[2]).criterion_spine_number is None

        # The Criterion film-page id goes with the match when Wikidata has
        # it. A missing id does not clear a field.

        assert db.session.get(Movie, ids[0]).criterion_film_id == "336-grand-illusion"
        assert db.session.get(Movie, ids[1]).criterion_film_id is None

        # A new match gets optimistic defaults for the fields that Wikidata
        # does not have.

        assert db.session.get(Movie, ids[0]).criterion_in_print is True
        assert db.session.get(Movie, ids[0]).criterion_disc_owned is False

    # Wikidata access etiquette: a User-Agent that identifies Fitzflix
    # with a contact, the SPARQL JSON accept header, and compression.

    assert len(calls) == 2
    headers = calls[0]["headers"]
    assert headers == calls[1]["headers"]
    assert headers["User-Agent"].startswith("FitzflixBot/")
    assert "mailto:" in headers["User-Agent"]
    assert headers["Accept"] == "application/sparql-results+json"
    assert "gzip" in headers["Accept-Encoding"]


def test_refresh_preserves_hand_set_fields(app, monkeypatch):
    fake_sparql(monkeypatch)

    with app.app_context():
        movie = make_movie(
            "Grand Illusion",
            1937,
            tmdb_id=1863,
            criterion_in_print=False,
            criterion_disc_owned=True,
            criterion_set_title="A Set I Typed Myself",
        )
        db.session.commit()
        movie_id = movie.id

        assert refresh_criterion_collection_info() is True

        db.session.expire_all()
        movie = db.session.get(Movie, movie_id)
        assert movie.criterion_spine_number == 1
        assert movie.criterion_in_print is False
        assert movie.criterion_disc_owned is True
        assert movie.criterion_set_title == "A Set I Typed Myself"


def test_single_movie_refresh_uses_cache(app, monkeypatch):
    """Make sure a one-movie refresh reads the cached list.

    The one-movie refresh is the per-import path. It does not query
    Wikidata again."""

    calls = []
    fake_sparql(monkeypatch, calls)

    with app.app_context():
        movie = make_movie("Seven Samurai", 1954)
        db.session.commit()
        movie_id = movie.id

        # Fill the cache with a full (forced) fetch.

        assert refresh_criterion_collection_info() is True
        assert len(calls) == 2

        # Redis serves a single-movie refresh.

        assert refresh_criterion_collection_info(movie_id=movie_id) is True
        assert len(calls) == 2

        db.session.expire_all()
        assert db.session.get(Movie, movie_id).criterion_spine_number == 2

        # The cached payload is well-formed JSON that holds the parsed rows.

        cached = json.loads(app.redis.get(videos.CRITERION_CACHE_KEY))
        assert {release["spine_number"] for release in cached} == {1, 2, 1000}


def test_refresh_survives_wikidata_outage(app, monkeypatch):
    """Make sure a failed fetch rolls back and logs, and does not raise."""

    import app.criterion_catalog as criterion_catalog

    def explode(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(criterion_catalog.requests, "get", explode)

    with app.app_context():
        movie = make_movie("Seven Samurai", 1954)
        db.session.commit()
        movie_id = movie.id

        assert refresh_criterion_collection_info() is not True

        db.session.expire_all()
        assert db.session.get(Movie, movie_id).criterion_spine_number is None


def test_criterion_page_row_grammar_and_badges(app, admin_client):
    """Make sure the Criterion page uses the search-row grammar.

    The page shows the library badge only when the user owns the disc AND
    the file matches the format of the release. In all other cases, the
    page shows an amber quality tier (find the Criterion version). The
    page also shows the personal funnel badges."""

    from app.models import User, UserMovieReview, UserWatchlist
    from app.recommendations import RECS_KEY
    from tests.factories import make_movie_file, quality

    with app.app_context():
        user_id = User.query.filter_by(admin=True).first().id
        bluray_id = quality("Bluray-1080p").id

        settled = make_movie(
            "Criterion Settled",
            1954,
            criterion_spine_number=100,
            criterion_disc_owned=True,
            criterion_quality_id=bluray_id,
            tmdb_overview="A settled classic.",
        )
        make_movie_file(settled, "Bluray-1080p")

        # The user owns the disc, but the rip is below the format of the
        # release.

        ripless = make_movie(
            "Criterion Disc Unripped",
            1960,
            criterion_spine_number=101,
            criterion_disc_owned=True,
            criterion_quality_id=bluray_id,
        )
        make_movie_file(ripless, "DVD")

        # The file is at the top format, but there is no Criterion disc.
        # The row stays amber.

        unowned = make_movie(
            "Criterion Unowned Disc",
            1965,
            criterion_spine_number=102,
            criterion_set_title="Essential Arthouse",
            criterion_disc_owned=False,
        )
        make_movie_file(unowned, "Bluray-2160p Remux")

        # An owned disc with a 1080p file counts as settled on this page.
        # This is true although Criterion released the film again in
        # 2160p. The shopping list is responsible for that upgrade (rule
        # set by Glenn).

        good_enough = make_movie(
            "Criterion Good Enough",
            1970,
            criterion_spine_number=103,
            criterion_disc_owned=True,
            criterion_quality_id=quality("Bluray-2160p Remux").id,
        )
        make_movie_file(good_enough, "Bluray-1080p")

        db.session.add(UserWatchlist(user_id=user_id, movie_id=ripless.id))
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=settled.id,
                rating=9,
                modified_rating=9,
                whole_stars=4,
                half_stars=1,
            )
        )
        db.session.commit()
        settled_id, unowned_id = settled.id, unowned.id

    # Both films are in the stored recommendations. The seen film must
    # not show the might-interest badge.

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 01:45",
                "items": [
                    {"movie_id": unowned_id, "score": 1.0, "because": []},
                    {"movie_id": settled_id, "score": 0.9, "because": []},
                ],
            }
        ),
    )

    page = admin_client.get("/library/criterion-collection").get_data(as_text=True)

    assert "#100 &ndash; Criterion Settled (1954)" in page

    # There are 2 settled rows: the format match, and the case of a 1080p
    # file against a 2160p release. The page counts the second case as
    # done on purpose.

    # The settled/amber answer is not on the tiles (#77a). The popover
    # shows it. The criterion context on the card URL sets its color.

    assert 'title="In your Fitzflix library"' not in page
    assert 'text-bg-warning me-1">DVD' not in page
    assert (
        f'data-card-url="/movie_card?movie_id={settled_id}&amp;context=criterion"'
        in page
    )
    assert f'data-state-movie="{settled_id}"' in page

    # The funnel is not on the tiles (2026-08). The hydrated widgets show
    # Seen and the watchlist answer. The might-interest label goes with
    # the anchor of the recommended film as a card label. A seen film
    # never gets the label, even if it is in the stored recommendations.

    assert 'text-bg-info me-1">Seen' not in page
    assert "On your watchlist" not in page
    assert page.count("Might interest you") == 1
    assert (
        f'data-card-url="/movie_card?movie_id={unowned_id}&amp;context=criterion" '
        "data-card-reasons='[\"Might interest you\"]'"
    ) in page
    assert "Part of the Essential Arthouse collector's set" in page
    # The poster popover shows the synopsis (#45d).
    assert "A settled classic." not in page
    assert f'href="/movie/{settled_id}"' in page

    # The criterion-context card uses the SETTLED rule for its color.
    # The card is green only when the user owns the disc AND the copy
    # meets the format of the release. The app threshold caps the
    # format. Thus, the owned disc with a 1080p file against a 2160p
    # re-release stays green. The DVD rip of an owned disc and the
    # unowned remux both go amber.

    with app.app_context():
        ripless_id = Movie.query.filter_by(title="Criterion Disc Unripped").one().id
        good_enough_id = Movie.query.filter_by(title="Criterion Good Enough").one().id

    def criterion_card(movie_id):
        return admin_client.get(
            f"/movie_card?movie_id={movie_id}&context=criterion"
        ).get_data(as_text=True)

    # Since 2026-08, the rule sets the color of the In-library badge. The
    # tier badge is not on the card.

    assert (
        'text-bg-success align-middle me-1" title="In your Fitzflix library'
        in criterion_card(settled_id)
    )
    assert (
        'text-bg-warning align-middle me-1" title="In your Fitzflix library'
        in criterion_card(ripless_id)
    )
    assert (
        'text-bg-warning align-middle me-1" title="In your Fitzflix library'
        in criterion_card(unowned_id)
    )
    assert (
        'text-bg-success align-middle me-1" title="In your Fitzflix library'
        in criterion_card(good_enough_id)
    )

    # Without the context, the same unowned remux is green. That is the
    # generic shopping answer. Thus, the new color applies only in the
    # context.

    generic = admin_client.get(f"/movie_card?movie_id={unowned_id}").get_data(
        as_text=True
    )
    assert (
        'text-bg-success align-middle me-1" title="In your Fitzflix library' in generic
    )


def _seed_release_cache(app, releases):
    """Store a canned spine catalog at the location that the page reads."""

    app.redis.set(videos.CRITERION_CACHE_KEY, json.dumps(releases))


def release(spine, title, year, tmdb_id=None, set_title=None):
    """Return one cached release dict in the shape that the Wikidata parser stores."""

    return {
        "spine_number": spine,
        "tmdb_id": tmdb_id,
        "title": title.upper(),
        "label": title,
        "year": year,
        "criterion_film_id": None,
        "set_title": set_title,
    }


def test_criterion_page_shows_full_catalog(app, admin_client):
    """Make sure the page lists the whole spine catalog.

    A library film consumes its release (by TMDB id, or by title and
    year). The page never shows a duplicate. An owned film that the
    refresh did not mark still gets a row with its catalog spine. A
    release that is not in the library renders as a log-page link, with
    funnel badges from a local record if there is one. A release with no
    TMDB id renders as a plain row. The Criterion Channel badge marks the
    films that stream."""

    from app.models import User, UserWatchlist
    from app.streaming import AVAILABILITY_KEY
    from tests.factories import make_movie_file, quality

    with app.app_context():
        user_id = User.query.filter_by(admin=True).first().id
        bluray_id = quality("Bluray-1080p").id

        owned = make_movie(
            "Criterion Settled",
            1954,
            tmdb_id=555001,
            criterion_spine_number=100,
            criterion_disc_owned=True,
            criterion_quality_id=bluray_id,
        )
        make_movie_file(owned, "Bluray-1080p")

        # This film is in the library and in the catalog, but the refresh
        # did not stamp its criterion fields. The TMDB id connects them.

        unmarked = make_movie("Unmarked Owned", 1980, tmdb_id=555003)
        make_movie_file(unmarked, "Bluray-1080p")

        # This film has Criterion fields but no TMDB id. It consumes its
        # release by title and year. Thus, the catalog must not repeat it.

        by_title = make_movie(
            "Title Match", 1990, criterion_spine_number=500, criterion_disc_owned=False
        )
        make_movie_file(by_title, "DVD")

        # This is a record with no files for a catalog release (a film on
        # the watchlist, logged through TMDB). It dresses the row and
        # carries the funnel.

        record = make_movie(
            "Catalog Only",
            1962,
            tmdb_id=555002,
            tmdb_title="Catalog Only",
            tmdb_overview="A spine the library lacks.",
        )
        db.session.add(UserWatchlist(user_id=user_id, movie_id=record.id))
        db.session.commit()

    _seed_release_cache(
        app,
        [
            release(100, "Criterion Settled", 1954, tmdb_id=555001),
            release(200, "Catalog Only", 1962, tmdb_id=555002),
            release(300, "No Tmdb Release", 1971),
            release(400, "Unmarked Owned", 1980, tmdb_id=555003),
            release(500, "Title Match", 1990),
            # This is a box-set CONTAINER (the set item holds the spine and
            # has no TMDB id) and its member. Only the member can render.
            release(600, "Shadow Trilogy", 1944),
            release(
                600, "Shadow Part I", 1944, tmdb_id=555004, set_title="Shadow Trilogy"
            ),
        ],
    )

    # The catalog release streams on the Criterion Channel. The test
    # seeds the day cache. Thus, no TMDB call occurs.

    app.redis.set(
        AVAILABILITY_KEY.format(tmdb_id=555002),
        json.dumps(
            {
                "link": "https://example.test/watch",
                "flatrate": [
                    {
                        "provider_id": 258,
                        "provider_name": "The Criterion Channel",
                        "logo_path": "/criterion.jpg",
                    }
                ],
                "ads": [],
                "rent": [],
                "buy": [],
            }
        ),
    )

    page = admin_client.get("/library/criterion-collection").get_data(as_text=True)

    # Each spine gets exactly 1 row, in spine order.

    assert page.count("Criterion Settled (1954)") == 1
    assert page.count("Catalog Only (1962)") == 1
    assert page.count("Title Match (1990)") == 1
    assert "#300 &ndash; No Tmdb Release (1971)" in page
    assert "#400 &ndash; Unmarked Owned (1980)" in page
    assert (
        page.index("#100 &ndash;")
        < page.index("#200 &ndash;")
        < page.index("#300 &ndash;")
        < page.index("#400 &ndash;")
        < page.index("#500 &ndash;")
    )

    # The catalog row with a record opens its movie page directly. The
    # log page would only redirect there. The popover now shows its
    # funnel, overview, and streaming availability. Thus, the tile
    # carries the state container and not the badges.

    with app.app_context():
        record_id = Movie.query.filter_by(tmdb_id=555002).first().id
    assert f'href="/movie/{record_id}"' in page
    assert 'href="/review/tmdb/555002"' not in page
    assert "On your watchlist" not in page
    # The poster popover shows the synopsis (#45d). The card URL carries
    # the criterion context for the settled color (#77a).
    assert "A spine the library lacks." not in page
    assert (
        f'data-card-url="/movie_card?movie_id={record_id}&amp;context=criterion"'
        in page
    )
    assert f'data-state-movie="{record_id}"' in page
    assert 'title="Streaming on The Criterion Channel"' not in page

    # The box-set container never hides its members. The member renders
    # with its set line. The container row does not exist.

    assert "Shadow Part I (1944)" in page
    assert "Part of the Shadow Trilogy collector's set" in page
    assert "#600 &ndash; Shadow Trilogy" not in page

    # The plain row has no link.

    assert 'href="/review/tmdb/555003"' not in page  # the library row links home
    with app.app_context():
        unmarked_id = Movie.query.filter_by(tmdb_id=555003).first().id
    assert f'href="/movie/{unmarked_id}"' in page

    # The filters: "library" removes the catalog rows. "settled" keeps
    # only the settled library rows. The unmarked film has no owned disc.
    # The DVD rip is below its release. Thus, only the settled film
    # remains.

    library_page = admin_client.get(
        "/library/criterion-collection?filter=library"
    ).get_data(as_text=True)
    assert "Catalog Only (1962)" not in library_page
    assert "No Tmdb Release" not in library_page
    assert "Unmarked Owned (1980)" in library_page

    settled_page = admin_client.get(
        "/library/criterion-collection?filter=settled"
    ).get_data(as_text=True)
    assert "Criterion Settled (1954)" in settled_page
    assert "Unmarked Owned (1980)" not in settled_page
    assert "Title Match (1990)" not in settled_page


def test_full_refresh_creates_catalog_records(app, monkeypatch):
    """Make sure a full refresh creates records for unknown spine releases.

    The records have no files. They use the Wikidata label and have the
    criterion fields stamped. The refresh queues the standard TMDB
    refresh for them. That refresh renames them to the canonical TMDB
    title. Thus, later imports match. The full refresh adopts a record
    that matches by title and year but has no TMDB id. It skips a release
    with no TMDB id. The single-movie path never creates a record."""

    fake_sparql(monkeypatch)

    with app.app_context():
        # The refresh adopts an existing record with the title and year of
        # the release but no TMDB id. It does not create a duplicate.

        adoptee = make_movie("All Monsters Attack", 1969)
        db.session.commit()
        adoptee_id = adoptee.id

        assert refresh_criterion_collection_info() is True

        db.session.expire_all()
        adoptee = db.session.get(Movie, adoptee_id)
        assert adoptee.tmdb_id == 39462
        assert adoptee.criterion_spine_number == 1000
        assert adoptee.criterion_set_title == "Godzilla: The Showa-Era Films, 1954-1975"

        # The unknown release is now a record, with the label case and
        # the criterion fields stamped.

        created = Movie.query.filter_by(tmdb_id=1863).first()
        assert created is not None
        assert created.title == "La Grande Illusion"
        assert created.year == 1937
        assert created.criterion_spine_number == 1
        assert created.files.count() == 0

        # The Seven Samurai release has no TMDB id. It created nothing.

        assert Movie.query.filter_by(title="Seven Samurai").first() is None

        # The new record and the adopted record both queued a TMDB refresh.

        jobs = app.maintenance_queue.jobs
        refreshed_ids = {
            job.args[1]
            for job in jobs
            if job.func_name == "app.videos.refresh_tmdb_info"
        }
        assert refreshed_ids == {created.id, adoptee_id}

        # The single-movie path never creates a record.

        before = Movie.query.count()
        assert refresh_criterion_collection_info(movie_id=adoptee_id) is True
        assert Movie.query.count() == before


def test_catalog_exclusion_blocks_recreation_and_rendering(
    app, monkeypatch, admin_client
):
    """Make sure `flask catalog exclude` deletes a bad record and blocks its TMDB id.

    A later full refresh does not create the record again. The catalog
    page stops rendering its release. A record with real library data
    refuses the command."""

    from tests.factories import make_movie_file

    fake_sparql(monkeypatch)

    # The fitzflix.py entrypoint attaches the CLI commands, not
    # create_app. Thus, the test app registers them itself.

    from app import cli as app_cli

    if "catalog" not in app.cli.commands:
        app_cli.register(app)

    with app.app_context():
        assert refresh_criterion_collection_info() is True
        movie = Movie.query.filter_by(tmdb_id=1863).first()
        assert movie is not None
        movie_id = movie.id

    runner = app.test_cli_runner()
    result = runner.invoke(args=["catalog", "exclude", str(movie_id)])
    assert result.exit_code == 0
    assert "Deleted" in result.output and "1863" in result.output

    with app.app_context():
        assert Movie.query.filter_by(tmdb_id=1863).first() is None

        # The next full refresh skips the excluded id. It does not create
        # the record again.

        assert refresh_criterion_collection_info() is True
        assert Movie.query.filter_by(tmdb_id=1863).first() is None

    # The catalog page does not render the release and does not link it.

    _seed_release_cache(app, [release(1, "La Grande Illusion", 1937, tmdb_id=1863)])
    page = admin_client.get("/library/criterion-collection").get_data(as_text=True)
    assert "La Grande Illusion" not in page

    # A record with files is library data. It is never catalog junk.

    with app.app_context():
        owned = make_movie("Exclusion Owned", 1960, tmdb_id=555009)
        make_movie_file(owned, "DVD")
        db.session.commit()
        owned_id = owned.id
    result = runner.invoke(args=["catalog", "exclude", str(owned_id)])
    assert "has files" in result.output
    with app.app_context():
        assert db.session.get(Movie, owned_id) is not None


def test_criterion_catalog_pagination():
    """Make sure the page-number window keeps the ends and the current area.

    The window marks the gaps."""

    from app.main.library import _page_window

    assert _page_window(1, 1) == [1]
    assert _page_window(2, 3) == [1, 2, 3]
    assert _page_window(6, 12) == [1, 2, None, 4, 5, 6, 7, 8, None, 11, 12]


def test_shopping_list_links_direct_to_criterion_film_page(
    app, admin_client, monkeypatch
):
    """Make sure a movie with a stored Criterion film id gets a direct link.

    The link opens the film page and not a criterion.com search."""

    from tests.factories import make_movie_file

    with app.app_context():
        movie = make_movie(
            "Grand Illusion",
            1937,
            criterion_spine_number=1,
            criterion_film_id="336-grand-illusion",
            criterion_in_print=True,
        )
        make_movie_file(movie, "DVD")
        db.session.commit()

    page = admin_client.get("/shopping-list/movie").get_data(as_text=True)
    assert "https://www.criterion.com/films/336-grand-illusion" in page


def test_box_set_members_get_set_spine_and_title(app, monkeypatch):
    """Make sure a film in a box set matches through the P527 membership.

    The film takes the spine and the title of the set, and its own
    film-page id. The refresh never overwrites a hand-set set title."""

    fake_sparql(monkeypatch)

    with app.app_context():
        member = make_movie("All Monsters Attack", 1969, tmdb_id=39462)
        curated = make_movie(
            "Godzilla's Revenge",
            1969,
            tmdb_id=39462,
            criterion_set_title="My Own Set Name",
        )
        db.session.commit()
        member_id, curated_id = member.id, curated.id

        assert refresh_criterion_collection_info() is True

        db.session.expire_all()
        member = db.session.get(Movie, member_id)
        assert member.criterion_spine_number == 1000
        assert member.criterion_set_title == "Godzilla: The Showa-Era Films, 1954-1975"
        assert member.criterion_film_id == "29306"

        curated = db.session.get(Movie, curated_id)
        assert curated.criterion_spine_number == 1000
        assert curated.criterion_set_title == "My Own Set Name"
