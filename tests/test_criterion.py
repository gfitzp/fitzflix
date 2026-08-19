"""Criterion Collection refresh from Wikidata: SPARQL response parsing, the
access etiquette headers, TMDb-id-first matching with title/year fallback,
preservation of hand-set fields, and the Redis cache.
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
                # Matched by TMDb id, even though the library title differs
                "spine": {"value": "1"},
                "tmdbId": {"value": "1863"},
                "filmLabel": {"value": "La Grande Illusion"},
                "year": {"value": "1937"},
                "criterionId": {"value": "336-grand-illusion"},
            },
            {
                # No TMDb id on Wikidata: matched by title and year
                "spine": {"value": "2"},
                "filmLabel": {"value": "Seven Samurai"},
                "year": {"value": "1954"},
            },
            {
                # Unparseable spine rows are skipped, not fatal
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
                # A box set: the spine and title belong to the set item,
                # the film details to the member
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
        # The library's title differs from Wikidata's label; only the TMDb
        # id can connect them

        by_id = make_movie("Grand Illusion", 1937, tmdb_id=1863)

        # No TMDb match yet: falls back to title and year

        by_title = make_movie("Seven Samurai", 1954)

        # Not a Criterion release: untouched

        other = make_movie("Sharknado", 2013, tmdb_id=119283)
        db.session.commit()
        ids = (by_id.id, by_title.id, other.id)

        assert refresh_criterion_collection_info() is True

        db.session.expire_all()
        assert db.session.get(Movie, ids[0]).criterion_spine_number == 1
        assert db.session.get(Movie, ids[1]).criterion_spine_number == 2
        assert db.session.get(Movie, ids[2]).criterion_spine_number is None

        # The Criterion film-page id rides along when Wikidata has it, and
        # its absence doesn't clear anything

        assert db.session.get(Movie, ids[0]).criterion_film_id == "336-grand-illusion"
        assert db.session.get(Movie, ids[1]).criterion_film_id is None

        # New matches get optimistic defaults for fields Wikidata lacks

        assert db.session.get(Movie, ids[0]).criterion_in_print is True
        assert db.session.get(Movie, ids[0]).criterion_disc_owned is False

    # Wikidata access etiquette: identifying User-Agent with a contact,
    # SPARQL JSON accept header, and compression

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
    """A one-movie refresh (the per-import path) reads the cached list
    instead of re-querying Wikidata."""

    calls = []
    fake_sparql(monkeypatch, calls)

    with app.app_context():
        movie = make_movie("Seven Samurai", 1954)
        db.session.commit()
        movie_id = movie.id

        # Prime the cache with a full (forced) fetch

        assert refresh_criterion_collection_info() is True
        assert len(calls) == 2

        # A single-movie refresh is served from Redis

        assert refresh_criterion_collection_info(movie_id=movie_id) is True
        assert len(calls) == 2

        db.session.expire_all()
        assert db.session.get(Movie, movie_id).criterion_spine_number == 2

        # The cached payload is well-formed JSON with the parsed rows

        cached = json.loads(app.redis.get(videos.CRITERION_CACHE_KEY))
        assert {release["spine_number"] for release in cached} == {1, 2, 1000}


def test_refresh_survives_wikidata_outage(app, monkeypatch):
    """A failed fetch rolls back and logs rather than raising."""

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
    """The Criterion page speaks the search-row grammar: the library
    badge only when the disc is owned AND the file matches the
    release's own format, an amber quality tier otherwise (go find the
    Criterion version), plus the personal funnel badges."""

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

        # Disc owned but the rip lags the release's format

        ripless = make_movie(
            "Criterion Disc Unripped",
            1960,
            criterion_spine_number=101,
            criterion_disc_owned=True,
            criterion_quality_id=bluray_id,
        )
        make_movie_file(ripless, "DVD")

        # Topped-out file, but no Criterion disc — still amber

        unowned = make_movie(
            "Criterion Unowned Disc",
            1965,
            criterion_spine_number=102,
            criterion_set_title="Essential Arthouse",
            criterion_disc_owned=False,
        )
        make_movie_file(unowned, "Bluray-2160p Remux")

        # Owned disc with a 1080p file counts as settled on this page
        # even though Criterion re-released it in 2160p — chasing that
        # upgrade is the shopping list's job (Glenn's rule)

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

    # Both films sit in the stored recommendations — the seen one must
    # not badge might-interest anyway

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

    # Two settled rows: the format match, and the 1080p-file-vs-2160p-
    # release case the page deliberately calls done

    assert page.count('title="In your Fitzflix library"') == 2
    assert 'text-bg-warning me-1">DVD' in page
    assert 'text-bg-warning me-1">Bluray-2160p Remux' in page
    assert 'text-bg-warning me-1">Bluray-1080p' not in page
    assert 'text-bg-info me-1">Seen' in page
    assert "On your watchlist" in page
    assert page.count("Might interest you") == 1
    assert page.index("Criterion Unowned Disc (1965)") < page.index(
        "Might interest you"
    )
    assert "Part of the Essential Arthouse collector's set" in page
    # Tiles keep the shopping answer; the synopsis lives in the
    # poster popover now (#45d), fetched via data-card-url
    assert "A settled classic." not in page
    assert f'data-card-url="/movie_card?movie_id={settled_id}"' in page
    assert f'href="/movie/{settled_id}"' in page


def _seed_release_cache(app, releases):
    """Store a canned spine catalog where the page reads it from."""

    app.redis.set(videos.CRITERION_CACHE_KEY, json.dumps(releases))


def release(spine, title, year, tmdb_id=None, set_title=None):
    """One cached release dict, the shape the Wikidata parser stores."""

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
    """The page lists the whole spine catalog: library films consume
    their releases (TMDb id or title+year, never duplicated), an owned
    film the refresh never marked still rows up with its catalog spine,
    releases beyond the library render as log-page links with funnel
    badges off any local record, TMDb-less releases render as plain
    rows, and the Criterion Channel badge marks what's streaming."""

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

        # In the library and in the catalog, but the refresh never
        # stamped its criterion fields — the TMDb id connects them

        unmarked = make_movie("Unmarked Owned", 1980, tmdb_id=555003)
        make_movie_file(unmarked, "Bluray-1080p")

        # Criterion-marked but TMDb-less: consumes its release by
        # title and year, so the catalog must not repeat it

        by_title = make_movie(
            "Title Match", 1990, criterion_spine_number=500, criterion_disc_owned=False
        )
        make_movie_file(by_title, "DVD")

        # A file-less record for a catalog release (a watchlisted film
        # logged through TMDb): dresses the row and carries the funnel

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
            # A box-set CONTAINER (the set item holds the spine, no TMDb
            # id) plus its member: only the member may render
            release(600, "Shadow Trilogy", 1944),
            release(
                600, "Shadow Part I", 1944, tmdb_id=555004, set_title="Shadow Trilogy"
            ),
        ],
    )

    # The catalog release is streaming on the Criterion Channel (day
    # cache seeded, so no TMDb call happens)

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

    # Every spine rows up exactly once, in spine order

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

    # The record-backed catalog row opens its movie page directly (the
    # log page would just redirect there), wears the record's funnel
    # badge and overview, and shows the Criterion Channel badge with
    # the mandatory JustWatch credit

    with app.app_context():
        record_id = Movie.query.filter_by(tmdb_id=555002).first().id
    assert f'href="/movie/{record_id}"' in page
    assert 'href="/review/tmdb/555002"' not in page
    assert "On your watchlist" in page
    # The synopsis moved into the poster popover (#45d)
    assert "A spine the library lacks." not in page
    assert f'data-card-url="/movie_card?movie_id={record_id}"' in page
    assert 'title="Streaming on The Criterion Channel"' in page
    assert "Streaming data by JustWatch" in page

    # The box-set container never shadows its members: the member
    # renders with its set line, the container row doesn't exist

    assert "Shadow Part I (1944)" in page
    assert "Part of the Shadow Trilogy collector's set" in page
    assert "#600 &ndash; Shadow Trilogy" not in page

    # The plain row has nothing to link

    assert 'href="/review/tmdb/555003"' not in page  # library row links home
    with app.app_context():
        unmarked_id = Movie.query.filter_by(tmdb_id=555003).first().id
    assert f'href="/movie/{unmarked_id}"' in page

    # The filters: "library" drops catalog rows, "settled" keeps only
    # settled library rows — the unmarked film has no owned disc, the
    # DVD rip lags its release, so only the settled film remains

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
    """A full refresh creates file-less records for spine releases the
    library has never seen — under the Wikidata label, with criterion
    fields stamped and the standard TMDb refresh queued (which renames
    them to TMDb's canonical title, so later imports match) — adopts
    title+year records that lack a TMDb id, skips TMDb-less releases,
    and never creates on the single-movie path."""

    fake_sparql(monkeypatch)

    with app.app_context():
        # An existing record with the release's title and year but no
        # TMDb id gets adopted rather than duplicated

        adoptee = make_movie("All Monsters Attack", 1969)
        db.session.commit()
        adoptee_id = adoptee.id

        assert refresh_criterion_collection_info() is True

        db.session.expire_all()
        adoptee = db.session.get(Movie, adoptee_id)
        assert adoptee.tmdb_id == 39462
        assert adoptee.criterion_spine_number == 1000
        assert adoptee.criterion_set_title == "Godzilla: The Showa-Era Films, 1954-1975"

        # The unknown release became a record, label-cased, stamped

        created = Movie.query.filter_by(tmdb_id=1863).first()
        assert created is not None
        assert created.title == "La Grande Illusion"
        assert created.year == 1937
        assert created.criterion_spine_number == 1
        assert created.files.count() == 0

        # The TMDb-less Seven Samurai release created nothing

        assert Movie.query.filter_by(title="Seven Samurai").first() is None

        # Both the new record and the adopted one queued a TMDb refresh

        jobs = app.maintenance_queue.jobs
        refreshed_ids = {
            job.args[1]
            for job in jobs
            if job.func_name == "app.videos.refresh_tmdb_info"
        }
        assert refreshed_ids == {created.id, adoptee_id}

        # The single-movie path never creates records

        before = Movie.query.count()
        assert refresh_criterion_collection_info(movie_id=adoptee_id) is True
        assert Movie.query.count() == before


def test_catalog_exclusion_blocks_recreation_and_rendering(
    app, monkeypatch, admin_client
):
    """`flask catalog exclude` deletes a bogus record and bars its TMDb
    id: later full refreshes don't recreate it, the catalog page stops
    rendering its release, and records with real library data refuse."""

    from tests.factories import make_movie_file

    fake_sparql(monkeypatch)

    # CLI commands attach in the fitzflix.py entrypoint, not create_app,
    # so the test app registers them itself

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

        # The next full refresh skips the excluded id instead of
        # recreating the record

        assert refresh_criterion_collection_info() is True
        assert Movie.query.filter_by(tmdb_id=1863).first() is None

    # The catalog page neither renders the release nor links it

    _seed_release_cache(app, [release(1, "La Grande Illusion", 1937, tmdb_id=1863)])
    page = admin_client.get("/library/criterion-collection").get_data(as_text=True)
    assert "La Grande Illusion" not in page

    # A record with files is library data, never catalog junk

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
    """The page-number window keeps the ends and the neighborhood of
    the current page, with gaps marked."""

    from app.main.library import _page_window

    assert _page_window(1, 1) == [1]
    assert _page_window(2, 3) == [1, 2, 3]
    assert _page_window(6, 12) == [1, 2, None, 4, 5, 6, 7, 8, None, 11, 12]


def test_shopping_list_links_direct_to_criterion_film_page(
    app, admin_client, monkeypatch
):
    """A movie with a stored Criterion film id gets a direct film-page link
    instead of a criterion.com search."""

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
    """A film inside a box set matches through the set's P527 membership:
    it takes the set's spine and title, and its own film-page id — but a
    hand-curated set title is never overwritten."""

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
