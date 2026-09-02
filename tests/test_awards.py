"""Test the film awards from Wikidata.

These tests cover the batched SPARQL refresh with its current-truth
replacement semantics, the person-item craft backfill that merges on
top of it, the movie-page award strip, and the capped quality prior
that the recommendation engine adds."""

from datetime import datetime

import pytest

from app import db
from app.models import MovieAward
from tests.factories import make_movie, make_movie_file
from tests.test_recommendations import admin_id, genre, log_watch


def binding(ext, award_q, label, kind, year=None):
    """Return a SPARQL result row in the shape that Wikidata returns."""

    row = {
        "ext": {"value": ext},
        "award": {"value": f"http://www.wikidata.org/entity/{award_q}"},
        "awardLabel": {"value": label},
        "kind": {"value": kind},
    }
    if year:
        row["year"] = {"value": str(year)}
    return row


def test_refresh_parses_batches_and_replaces(app, monkeypatch):
    """Test that the refresh parses the batches and replaces the rows.

    IMDb-matched films and TMDB-fallback films both resolve. Duplicate
    statements are removed. Label-service misses drop. When the response
    no longer lists a film, Fitzflix deletes its stale rows."""

    import app.awards as awards

    with app.app_context():
        by_imdb = make_movie("Awarded via IMDb", 1954, imdb_id="tt0047296")
        by_tmdb = make_movie("Awarded via TMDB", 1927, tmdb_id=901)
        unlisted = make_movie("Formerly Awarded", 1999, imdb_id="tt0000001")
        db.session.add(
            MovieAward(
                movie_id=unlisted.id,
                award_id="Q999",
                award_name="Stale Prize",
                win=True,
            )
        )
        db.session.commit()
        imdb_movie_id, tmdb_movie_id = by_imdb.id, by_tmdb.id

    def fake_sparql(query):
        """Return the canned bindings for each id-system query."""

        if "P345" in query:
            assert '"tt0047296"' in query and '"tt0000001"' in query
            return [
                binding(
                    "tt0047296",
                    "Q103618",
                    "Academy Award for Best Picture",
                    "win",
                    1955,
                ),
                # The same statement 2 times (re-import artifacts): the
                # duplicate is removed.
                binding(
                    "tt0047296",
                    "Q103618",
                    "Academy Award for Best Picture",
                    "win",
                    1955,
                ),
                binding(
                    "tt0047296",
                    "Q103360",
                    "BAFTA Award for Best Film",
                    "nomination",
                    1955,
                ),
                # A label-service miss echoes the QID back. That badge is useless.
                binding("tt0047296", "Q555", "Q555", "win"),
            ]
        assert "P4947" in query and '"901"' in query
        return [binding("901", "Q1011547", "National Board of Review Award", "win")]

    monkeypatch.setattr(awards, "_wikidata_sparql", fake_sparql)
    monkeypatch.setattr(awards.time, "sleep", lambda seconds: None)
    monkeypatch.setitem(
        app.config, "WIKIDATA_SPARQL_URL", "https://example.test/sparql"
    )

    with app.app_context():
        result = awards.refresh_movie_awards()
        stored = {
            (row.movie_id, row.award_name, row.win, row.year)
            for row in MovieAward.query.all()
        }

    assert stored == {
        (imdb_movie_id, "Academy Award for Best Picture", True, 1955),
        (imdb_movie_id, "BAFTA Award for Best Film", False, 1955),
        (tmdb_movie_id, "National Board of Review Award", True, None),
    }
    assert result == "Refreshed awards for 3 films, 2 with award records"


def test_craft_backfill_attributes_for_work_awards(app, monkeypatch):
    """Test that the craft backfill attributes for-work awards to films.

    Person items hold the craft categories. An award statement that
    names a library film as its "for work" attributes to the film
    through the ids of the film (the IMDb batch first, then the TMDB
    fallback). The rows merge without duplicates of what the film items
    already list. A film row without a year still suppresses its dated
    person copy. A win goes in when only the nomination is on record."""

    import app.awards as awards

    with app.app_context():
        waterfront = make_movie(
            "Backfill Waterfront", 1954, imdb_id="tt0047296", tmdb_id=654
        )
        ryan = make_movie("Backfill Ryan", 1998, tmdb_id=857)

        # This is what the film pass already found on the item of the film.

        db.session.add_all(
            [
                MovieAward(
                    movie_id=waterfront.id,
                    award_id="Q103618",
                    award_name="Academy Award for Best Picture",
                    win=True,
                    year=1955,
                ),
                MovieAward(
                    movie_id=waterfront.id,
                    award_id="Q106291",
                    award_name="BAFTA Award for Best Film",
                    win=False,
                ),
                MovieAward(
                    movie_id=waterfront.id,
                    award_id="Q103360",
                    award_name="Academy Award for Best Directing",
                    win=False,
                    year=1955,
                ),
            ]
        )
        db.session.commit()
        waterfront_id, ryan_id = waterfront.id, ryan.id

    def fake_sparql(query):
        """Return the canned for-work bindings for each id-system query."""

        assert "pq:P1686" in query
        if "P345" in query:
            assert '"tt0047296"' in query
            return [
                # This is the craft win that the film pass misses.
                binding(
                    "tt0047296",
                    "Q103360",
                    "Academy Award for Best Directing",
                    "win",
                    1955,
                ),
                # The same award through a second honoree (2 writers, 1
                # screenplay) is removed as a duplicate.
                binding(
                    "tt0047296",
                    "Q103360",
                    "Academy Award for Best Directing",
                    "win",
                    1955,
                ),
                # The film item already lists this win: an exact duplicate.
                binding(
                    "tt0047296",
                    "Q103618",
                    "Academy Award for Best Picture",
                    "win",
                    1955,
                ),
                # The film item has this award with no recorded year.
                # The dated person copy is still the same event.
                binding(
                    "tt0047296",
                    "Q106291",
                    "BAFTA Award for Best Film",
                    "nomination",
                    1955,
                ),
                # A label-service miss echoes the QID back. That badge is useless.
                binding("tt0047296", "Q555", "Q555", "win", 1955),
            ]
        assert "P4947" in query and '"857"' in query
        return [
            # The film without an IMDb id resolves through the TMDB batch.
            binding(
                "857",
                "Q131520",
                "Academy Award for Best Cinematography",
                "win",
                1999,
            ),
        ]

    monkeypatch.setattr(awards, "_wikidata_sparql", fake_sparql)
    monkeypatch.setattr(awards.time, "sleep", lambda seconds: None)
    monkeypatch.setitem(
        app.config, "WIKIDATA_SPARQL_URL", "https://example.test/sparql"
    )

    with app.app_context():
        result = awards.refresh_person_awards()
        stored = {
            (row.movie_id, row.award_id, row.win, row.year)
            for row in MovieAward.query.all()
        }

    assert stored == {
        (waterfront_id, "Q103618", True, 1955),
        (waterfront_id, "Q106291", False, None),
        (waterfront_id, "Q103360", False, 1955),
        (waterfront_id, "Q103360", True, 1955),
        (ryan_id, "Q131520", True, 1999),
    }
    assert result == "Scanned 2 films for craft awards, added 2 records for 2 films"

    # Standalone reruns are idempotent. Every row is now a duplicate.

    with app.app_context():
        rerun = awards.refresh_person_awards()
        assert MovieAward.query.count() == 5
    assert rerun == "Scanned 2 films for craft awards, added 0 records for 0 films"


def test_award_prior_math(app):
    """Test the math of the award prior.

    The prior weighs wins more than nominations, and it has a cap. The
    chip text prefers wins."""

    from app.recommendations import (
        AWARD_PRIOR_CAP,
        award_label,
        award_prior,
    )

    assert award_prior(1, 0) == pytest.approx(0.1)
    assert award_prior(0, 2) == pytest.approx(0.05)
    assert award_prior(50, 100) == AWARD_PRIOR_CAP
    assert award_label(2, 5) == "won 2 awards"
    assert award_label(1, 0) == "won 1 award"
    assert award_label(0, 3) == "award-nominated"


def test_award_prior_reranks_and_explains(app):
    """Test that the award prior reranks the candidates and explains why.

    When the taste profile scores 2 candidates the same, the awarded
    film ranks first and says why. Awards alone never recommend a film
    that the profile scores at 0."""

    from app.recommendations import compute_user_recommendations

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")

        liked = make_movie("Prior Liked Comedy", 1990)
        liked.genres.append(comedy)
        log_watch(user_id, liked, liked=True)

        plain = make_movie("Prior Plain Comedy", 1991)
        plain.genres.append(comedy)
        make_movie_file(plain, "Bluray-1080p")

        awarded = make_movie("Prior Awarded Comedy", 1992)
        awarded.genres.append(comedy)
        make_movie_file(awarded, "Bluray-1080p")
        db.session.add(
            MovieAward(
                movie_id=awarded.id,
                award_id="Q103618",
                award_name="Academy Award for Best Picture",
                win=True,
                year=1993,
            )
        )

        # This film has many awards, but it does not match the taste
        # (different genre AND decade, like the drama in the engine
        # tests). The taste gates the prior.

        drama = genre(18, "Drama")
        mismatched = make_movie("Prior Awarded Mismatch", 1953)
        mismatched.genres.append(drama)
        make_movie_file(mismatched, "Bluray-1080p")
        db.session.add(
            MovieAward(
                movie_id=mismatched.id,
                award_id="Q103618",
                award_name="Academy Award for Best Picture",
                win=True,
                year=1954,
            )
        )
        db.session.flush()

        profile, ranked, _ = compute_user_recommendations(user_id)
        ranked_ids = [rec["movie_id"] for rec in ranked]

        assert ranked_ids == [awarded.id, plain.id]
        assert "won 1 award" in ranked[0]["because"]
        assert "won 1 award" not in ranked[1]["because"]


def test_movie_page_shows_award_strip(app, admin_client):
    """Test that the movie page shows the award strip.

    The page lists the wins first, with the trophy. It marks the
    nominations. It credits Wikidata."""

    with app.app_context():
        movie = make_movie("Award Strip Film", 1954, tmdb_data_as_of=datetime.utcnow())
        make_movie_file(movie, "Bluray-1080p")
        db.session.add(
            MovieAward(
                movie_id=movie.id,
                award_id="Q103360",
                award_name="BAFTA Award for Best Film",
                win=False,
                year=1955,
            )
        )
        db.session.add(
            MovieAward(
                movie_id=movie.id,
                award_id="Q103618",
                award_name="Academy Award for Best Picture",
                win=True,
                year=1955,
            )
        )
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Academy Award for Best Picture (1955)" in page
    assert "BAFTA Award for Best Film (1955) &mdash; nominated" in page
    assert "bi-trophy-fill" in page
    assert "Award data from Wikidata" in page

    # Wins lead the strip. The alphabetical order is not important.

    assert page.index("Academy Award for Best Picture (1955)") < page.index(
        "BAFTA Award for Best Film (1955)"
    )


def test_sparql_client_honors_retry_after_on_429(app, monkeypatch):
    """Test that the SPARQL client honors Retry-After on a 429.

    A throttled query waits for the Retry-After of the 429 (capped) and
    retries 1 time. The WDQS manual sets this condition for a client
    that does not get banned."""

    import app.awards as awards
    import app.criterion_catalog as criterion_catalog
    from app.videos import wikidata_retry_after_seconds

    class FakeResponse:
        def __init__(self, status_code, headers=None, bindings=None):
            self.status_code = status_code
            self.headers = headers or {}
            self._bindings = bindings or []

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return {"results": {"bindings": self._bindings}}

    # The header parsing: seconds, absent, date-shaped, and the cap.

    assert wikidata_retry_after_seconds(FakeResponse(429, {"Retry-After": "3"})) == 3
    assert wikidata_retry_after_seconds(FakeResponse(429, {})) == 60
    assert (
        wikidata_retry_after_seconds(
            FakeResponse(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        )
        == 60
    )
    assert (
        wikidata_retry_after_seconds(FakeResponse(429, {"Retry-After": "9000"})) == 300
    )

    # Both clients: a 429, then success. One capped sleep occurs, then the
    # rows come.

    with app.app_context():
        for module, call in (
            (awards, lambda: awards._wikidata_sparql("SELECT 1")),
            (
                criterion_catalog,
                lambda: criterion_catalog._wikidata_sparql(
                    "http://example.test", "SELECT 1"
                ),
            ),
        ):
            responses = [
                FakeResponse(429, {"Retry-After": "3"}),
                FakeResponse(200, bindings=[{"ok": {"value": "1"}}]),
            ]
            sleeps = []
            monkeypatch.setattr(
                module.requests, "get", lambda *a, **k: responses.pop(0)
            )
            monkeypatch.setattr(module.time, "sleep", sleeps.append)
            assert call() == [{"ok": {"value": "1"}}]
            assert sleeps == [3]


def test_awards_refresh_aborts_after_consecutive_failures(app, monkeypatch):
    """Test that the refresh stops after consecutive failures.

    When Wikidata fails batch after batch, the refresh waits the longer
    error pause between tries. Then it stops fully. It does not send
    error query after error query. The weekly schedule heals itself."""

    from tests.factories import make_movie

    import app.awards as awards
    from app import db

    with app.app_context():
        for n in range(10):
            make_movie(f"Breaker Film {n}", 1990 + n, imdb_id=f"tt00000{n:02d}")
        db.session.commit()

        calls = []

        def explode(query):
            calls.append(query)
            raise RuntimeError("504 whoops")

        sleeps = []
        monkeypatch.setattr(awards, "_wikidata_sparql", explode)
        monkeypatch.setattr(awards, "AWARDS_BATCH_SIZE", 1)
        monkeypatch.setattr(awards.time, "sleep", sleeps.append)

        message = awards.refresh_movie_awards()

    assert len(calls) == awards.AWARDS_MAX_CONSECUTIVE_FAILURES
    assert "aborted" in message
    assert sleeps == [awards.AWARDS_ERROR_PAUSE_SECONDS] * (
        awards.AWARDS_MAX_CONSECUTIVE_FAILURES - 1
    )


def test_award_badges_can_wrap(app, admin_client):
    """Test that an award badge can wrap (#199).

    The .badge class of Bootstrap sets white-space: nowrap. Thus, a
    long award name made a badge wider than a phone, and the whole page
    scrolled sideways. The badge was 939px in a 375px viewport, 579px of
    overflow. The badge must carry the wrapping utility to override
    that."""

    from html.parser import HTMLParser

    long_name = "Golden Reel Award for Outstanding Achievement in Sound Editing - Sound"

    with app.app_context():
        movie = make_movie("Wrapping Film", 1999, tmdb_data_as_of=datetime.utcnow())
        make_movie_file(movie, "Bluray-1080p")
        db.session.add(
            MovieAward(
                movie_id=movie.id,
                award_id="Q123456",
                award_name=long_name,
                win=True,
                year=2000,
            )
        )
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert long_name in page

    class Badges(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.classes = []
            self.depth = 0

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "span" and "badge" in (attrs.get("class") or ""):
                self.classes.append(attrs["class"].split())

    badges = Badges()
    badges.feed(page)
    award_badges = [c for c in badges.classes if "text-bg-light" in c]
    assert award_badges, "no award badge on the page"
    for classes in award_badges:
        assert "text-wrap" in classes, "the badge would overflow instead of wrapping"
