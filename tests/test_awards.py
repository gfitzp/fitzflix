"""Film awards from Wikidata: the batched SPARQL refresh with its
current-truth replacement semantics, the person-item craft backfill
that merges on top of it, the movie-page award strip, and the capped
quality prior the recommendation engine folds in."""

from datetime import datetime

import pytest

from app import db
from app.models import MovieAward, MovieCast, MovieCrew, TMDBCredit
from tests.factories import make_movie, make_movie_file
from tests.test_recommendations import admin_id, genre, log_watch


def binding(ext, award_q, label, kind, year=None):
    """A SPARQL result row the shape Wikidata returns."""

    row = {
        "ext": {"value": ext},
        "award": {"value": f"http://www.wikidata.org/entity/{award_q}"},
        "awardLabel": {"value": label},
        "kind": {"value": kind},
    }
    if year:
        row["year"] = {"value": str(year)}
    return row


def person_binding(award_q, label, kind, year=None, imdb=None, tmdb=None):
    """A person-pass SPARQL row: the for-work film's ids, no person."""

    row = {
        "award": {"value": f"http://www.wikidata.org/entity/{award_q}"},
        "awardLabel": {"value": label},
        "kind": {"value": kind},
    }
    if year:
        row["year"] = {"value": str(year)}
    if imdb:
        row["workImdb"] = {"value": imdb}
    if tmdb:
        row["workTmdb"] = {"value": tmdb}
    return row


def test_refresh_parses_batches_and_replaces(app, monkeypatch):
    """IMDb-matched and TMDb-fallback films both resolve, duplicate
    statements dedupe, label-service misses drop, and films the
    response no longer lists have their stale rows wiped."""

    import app.awards as awards

    with app.app_context():
        by_imdb = make_movie("Awarded via IMDb", 1954, imdb_id="tt0047296")
        by_tmdb = make_movie("Awarded via TMDb", 1927, tmdb_id=901)
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
        """Canned bindings per id-system query."""

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
                # The same statement twice (re-import artifacts) dedupes
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
                # A label-service miss echoes the QID back; useless badge
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


def test_person_backfill_attributes_craft_awards(app, monkeypatch):
    """Craft awards on person items attribute to their for-work films
    (IMDb id first, TMDb fallback), merge without duplicating what the
    film items already list — year-less film rows still suppress their
    dated person copies, but a win lands when only the nomination is
    on record — and only credited cast + key crew are queried."""

    import app.awards as awards

    with app.app_context():
        waterfront = make_movie(
            "Backfill Waterfront", 1954, imdb_id="tt0047296", tmdb_id=654
        )
        ryan = make_movie("Backfill Ryan", 1998, tmdb_id=857)

        # What the film pass already found on the film's own item

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

        db.session.add_all(
            [
                TMDBCredit(id=951001, name="Backfill Director"),
                TMDBCredit(id=951002, name="Backfill Actor"),
                TMDBCredit(id=951003, name="Backfill Extra"),
                TMDBCredit(id=951004, name="Backfill Gaffer"),
            ]
        )
        db.session.add_all(
            [
                MovieCrew(
                    movie_id=waterfront.id,
                    credit_id=951001,
                    department="Directing",
                    job="Director",
                ),
                MovieCast(
                    movie_id=waterfront.id,
                    credit_id=951002,
                    character="Terry",
                    billing_order=0,
                ),
                MovieCast(
                    movie_id=waterfront.id,
                    credit_id=951003,
                    character="Dock Worker (uncredited)",
                    billing_order=40,
                ),
                MovieCrew(
                    movie_id=waterfront.id,
                    credit_id=951004,
                    department="Lighting",
                    job="Gaffer",
                ),
            ]
        )
        db.session.commit()
        waterfront_id, ryan_id = waterfront.id, ryan.id

    def fake_sparql(query):
        """Canned person-item bindings; the id set must stay bounded."""

        assert "P4985" in query
        assert '"951001"' in query and '"951002"' in query
        assert '"951003"' not in query and '"951004"' not in query
        return [
            # The craft win the film pass misses, by the work's IMDb id
            person_binding(
                "Q103360",
                "Academy Award for Best Directing",
                "win",
                1955,
                imdb="tt0047296",
                tmdb="654",
            ),
            # The same statement via a second honoree dedupes
            person_binding(
                "Q103360",
                "Academy Award for Best Directing",
                "win",
                1955,
                imdb="tt0047296",
                tmdb="654",
            ),
            # The film item already lists this win: exact duplicate
            person_binding(
                "Q103618",
                "Academy Award for Best Picture",
                "win",
                1955,
                imdb="tt0047296",
            ),
            # The film item has this award with no year recorded: the
            # dated person copy is still the same event
            person_binding(
                "Q106291",
                "BAFTA Award for Best Film",
                "nomination",
                1955,
                imdb="tt0047296",
            ),
            # No IMDb id on the work item: TMDb fallback resolves it
            person_binding(
                "Q131520",
                "Academy Award for Best Cinematography",
                "win",
                1999,
                tmdb="857",
            ),
            # For-work outside the library never attaches
            person_binding(
                "Q103916",
                "Academy Award for Best Actor",
                "win",
                1973,
                imdb="tt0068646",
                tmdb="238",
            ),
            # A label-service miss echoes the QID back; useless badge
            person_binding("Q555", "Q555", "win", 1955, imdb="tt0047296"),
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
    assert result == (
        "Scanned 2 credited people, added 2 craft award records for 2 films"
    )

    # Standalone reruns are idempotent: everything now dedupes

    with app.app_context():
        rerun = awards.refresh_person_awards()
        assert MovieAward.query.count() == 5
    assert rerun == (
        "Scanned 2 credited people, added 0 craft award records for 0 films"
    )


def test_award_prior_math(app):
    """The prior weighs wins over nominations and caps, and the chip
    text prefers wins."""

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
    """Between two candidates the taste profile scores identically, the
    awarded film ranks first and says why — but awards alone never
    recommend a film the profile scores at zero."""

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

        # Heavily awarded but taste-mismatched (different genre AND
        # decade, like the engine tests' drama): taste gates the prior

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

        profile, ranked = compute_user_recommendations(user_id)
        ranked_ids = [rec["movie_id"] for rec in ranked]

        assert ranked_ids == [awarded.id, plain.id]
        assert "won 1 award" in ranked[0]["because"]
        assert "won 1 award" not in ranked[1]["because"]


def test_movie_page_shows_award_strip(app, admin_client):
    """The movie page lists wins first with the trophy, marks
    nominations, and credits Wikidata."""

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

    # Wins lead the strip regardless of alphabetical order

    assert page.index("Academy Award for Best Picture (1955)") < page.index(
        "BAFTA Award for Best Film (1955)"
    )
