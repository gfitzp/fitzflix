"""Test the "since you liked…" strip and the live rating ladder.

The tests cover the candidate pool exclusions, the adjacency-ranked
suggestions after a positive movie-page rating, and the background
posts of the ladder."""

import re

from app import db
from app.models import MovieCrew, UserMovieReview, UserWatchlist
from tests.factories import make_movie, make_movie_file
from tests.test_recommendations import (
    admin_id,
    genre,
    log_watch,
    make_person,
)


def csrf_token_from(page_html):
    """Return the CSRF token from a rendered form."""

    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def make_candidate(title, year, genre_row=None, director=None):
    """Create a library film that is eligible for the drive, with optional features."""

    movie = make_movie(title, year)
    make_movie_file(movie, "Bluray-1080p")
    if genre_row is not None:
        movie.genres.append(genre_row)
    if director is not None:
        db.session.add(
            MovieCrew(
                movie_id=movie.id,
                credit_id=director.id,
                department="Directing",
                job="Director",
            )
        )
    return movie


def test_candidates_exclude_declared_states(app):
    from app.elicitation import elicitation_candidates
    from app.models import UserMovieStatus

    with app.app_context():
        user_id = admin_id()
        eligible = make_candidate("Elicit Eligible", 1990)
        logged = make_candidate("Elicit Logged", 1991)
        log_watch(user_id, logged, rating=4)
        wanted = make_candidate("Elicit Wanted", 1992)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))
        unseen = make_candidate("Elicit Unseen", 1993)
        db.session.flush()
        # A "No Opinion" row from the retired /rate drive still excludes
        # its film while the row is fresh.
        db.session.add(
            UserMovieStatus(user_id=user_id, movie_id=unseen.id, kind="unseen")
        )
        db.session.commit()

        assert elicitation_candidates(user_id) == [eligible.id]


def test_unseen_mark_expires_after_two_years(app):
    """Make sure the "Haven't seen it" mark expires.

    A mark older than the resurface bar stops the exclusion of the film.
    The user can have seen the film since then, or can remember a
    verdict."""

    from datetime import datetime, timedelta

    from app.elicitation import (
        UNSEEN_RESURFACE_YEARS,
        elicitation_candidates,
    )
    from app.models import UserMovieStatus

    with app.app_context():
        user_id = admin_id()
        film = make_candidate("Unseen Expiry", 1972)
        db.session.flush()
        film_id = film.id
        row = UserMovieStatus(user_id=user_id, movie_id=film_id, kind="unseen")
        db.session.add(row)
        db.session.commit()

        assert film_id not in elicitation_candidates(user_id)

        # Make the mark older than the bar. The film shows again.

        row.date_added = datetime.now() - timedelta(
            days=UNSEEN_RESURFACE_YEARS * 365.25 + 30
        )
        db.session.commit()
        assert film_id in elicitation_candidates(user_id)


def test_movie_page_ladder_logs_a_quick_rating(app, admin_client):
    """Make sure the ladder on the movie page logs a quick rating.

    The ladder goes with the log form of the movie page too. One tap
    logs a rating through the standard review path. The post keeps the
    other form fields as submitted."""

    with app.app_context():
        user_id = admin_id()
        movie = make_candidate("Ladder Movie Page", 1970)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert page.count("&#9734;") == 5
    token = csrf_token_from(page)

    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "quick_rating": "4"},
    )
    assert response.status_code == 302
    with app.app_context():
        review = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=movie_id
        ).one()
        assert float(review.rating) == 4.0


def test_movie_page_positive_rating_earns_suggestions(app, admin_client):
    """Make sure a positive rating on the movie page earns suggestions.

    A ladder tap of 3 or more earns the "since you liked" strip on the
    redirect back to that page, and only that page. A banked suggestion
    joins the watchlist. The anchor does not move."""

    import json

    from app.elicitation import last_response

    with app.app_context():
        user_id = admin_id()
        western = genre(37, "Western")
        auteur = make_person(777006, "Page Strip Director")
        anchor = make_candidate(
            "Page Strip Anchor", 1960, genre_row=western, director=auteur
        )
        similar = make_candidate(
            "Page Strip Similar", 1961, genre_row=western, director=auteur
        )
        similar_two = make_candidate(
            "Page Strip Similar Two", 1962, genre_row=western, director=auteur
        )
        drama = genre(18, "Drama")
        unrelated = make_candidate("Page Strip Unrelated", 1999, genre_row=drama)
        db.session.commit()
        anchor_id, similar_id = anchor.id, similar.id
        similar_two_id, unrelated_id = similar_two.id, unrelated.id

    app.redis.set(
        f"fitzflix:recs:profile:{user_id}",
        json.dumps(
            {
                "affinities": {
                    "genre:37": {
                        "class": "genre",
                        "label": "Western",
                        "count": 3,
                        "score": 0.5,
                    }
                },
                "movies": 3,
            }
        ),
    )

    page = admin_client.get(f"/movie/{anchor_id}").get_data(as_text=True)
    token = csrf_token_from(page)
    response = admin_client.post(
        f"/movie/{anchor_id}",
        data={"csrf_token": token, "quick_rating": "4"},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "Since you liked Page Strip Anchor" in body
    assert "Page Strip Similar (1961)" in body

    # Only the page of the anchor shows the strip.

    other = admin_client.get(f"/movie/{unrelated_id}").get_data(as_text=True)
    assert "Since you liked" not in other

    # A banked suggestion goes on the watchlist. The anchor and the
    # steering do not change.

    response = admin_client.post(
        f"/movie/{anchor_id}",
        data={
            "csrf_token": token,
            "movie_id": str(similar_id),
            "add_watchlist_submit": "Add to Watchlist",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        assert (
            UserWatchlist.query.filter_by(user_id=user_id, movie_id=similar_id).first()
            is not None
        )
        assert last_response(app.redis, user_id)["movie_id"] == anchor_id

    # The first GET renders the one-shot flash that names the film. The
    # second GET shows the steady state. The banked film left the strip.
    # Its sibling remains.

    admin_client.get(f"/movie/{anchor_id}")
    page = admin_client.get(f"/movie/{anchor_id}").get_data(as_text=True)
    assert "Page Strip Similar (1961)" not in page
    assert "Page Strip Similar Two (1962)" in page

    # A rating of a suggestion from the strip rates THAT film, with no
    # date. The anchor does not move. The strip refreshes without the
    # film.

    response = admin_client.post(
        f"/movie/{anchor_id}",
        data={
            "csrf_token": token,
            "movie_id": str(similar_two_id),
            "quick_rating": "2",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        row = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=similar_two_id
        ).one()
        assert float(row.rating) == 2.0
        assert row.date_watched is None
        assert last_response(app.redis, user_id)["movie_id"] == anchor_id

    admin_client.get(f"/movie/{anchor_id}")
    page = admin_client.get(f"/movie/{anchor_id}").get_data(as_text=True)
    assert "Page Strip Similar Two (1962)" not in page


def test_card_rating_on_suggestion_never_reanchors(app, admin_client):
    """Make sure a card rating on a suggestion never moves the anchor.

    A ladder tap on the poster tile of a suggestion (from_card) rates
    that film. The strip keeps its anchor. Only a rating on the own page
    of the film moves the session along (rule set by Glenn, 2026-08)."""

    import json

    from app.elicitation import last_response

    with app.app_context():
        user_id = admin_id()
        western = genre(37, "Western")
        auteur = make_person(777007, "Chain Director")
        anchor = make_candidate(
            "Chain Anchor", 1960, genre_row=western, director=auteur
        )
        similar = make_candidate(
            "Chain Similar", 1961, genre_row=western, director=auteur
        )
        db.session.commit()
        anchor_id, similar_id = anchor.id, similar.id

    app.redis.set(
        f"fitzflix:recs:profile:{user_id}",
        json.dumps(
            {
                "affinities": {
                    "genre:37": {
                        "class": "genre",
                        "label": "Western",
                        "count": 3,
                        "score": 0.5,
                    }
                },
                "movies": 3,
            }
        ),
    )

    page = admin_client.get(f"/movie/{anchor_id}").get_data(as_text=True)
    token = csrf_token_from(page)
    admin_client.post(
        f"/movie/{anchor_id}",
        data={"csrf_token": token, "quick_rating": "4"},
    )

    strip = admin_client.get(f"/movie/{anchor_id}").get_data(as_text=True)
    assert "Chain Similar (1961)" in strip

    # The ladder of the card posts to the movie route of the suggestion
    # with the from_card marker. It gets ladder-state JSON back.

    response = admin_client.post(
        f"/movie/{similar_id}",
        data={"csrf_token": token, "from_card": "1", "quick_rating": "5"},
        headers={"X-Requested-With": "ladder"},
    )
    assert response.status_code == 200
    state = response.get_json()
    assert state["rating"] == 5.0
    assert state["on_watchlist"] is False
    with app.app_context():
        row = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=similar_id
        ).one()
        assert float(row.rating) == 5.0

        # The anchor did not move. The strip stays the reward of the
        # rated featured film, without the suggestion that is now rated.

        last = last_response(app.redis, user_id)
        assert last["movie_id"] == anchor_id
        assert last["positive"] is True


def test_movie_page_x_flags_and_never_reviews(app, admin_client):
    """Make sure the ✕ on the movie page flags the film and never reviews it.

    The ✕ of the ladder writes the not-interested flag and no diary row.
    It clears a watchlist entry that contradicts the flag. A later log
    of the film clears the flag again."""

    from app.models import UserMovieStatus, UserWatchlist

    with app.app_context():
        user_id = admin_id()
        movie = make_candidate("X Owned Film", 1975)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert 'name="quick_rating" value="0"' in page
    token = csrf_token_from(page)
    admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "0"},
    )
    with app.app_context():
        assert (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).first()
            is None
        )
        assert (
            UserMovieStatus.query.filter_by(
                user_id=user_id, movie_id=movie_id, kind="not_interested"
            ).first()
            is not None
        )
        assert (
            UserWatchlist.query.filter_by(user_id=user_id, movie_id=movie_id).first()
            is None
        )

    # A watch contradicts the flag. The log clears the flag.

    admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "4"},
    )
    with app.app_context():
        review = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=movie_id
        ).one()
        assert float(review.rating) == 4.0
        assert (
            UserMovieStatus.query.filter_by(
                user_id=user_id, movie_id=movie_id, kind="not_interested"
            ).first()
            is None
        )


def test_seen_films_cannot_be_flagged_and_hide_the_x(app, admin_client):
    """Make sure a seen film refuses the ✕ and hides the button.

    The server refuses the ✕ for a film with a diary row. The floor of
    that film is 1 star. The ladder does not offer the button."""

    from app.models import UserMovieStatus
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = admin_id()
        movie = make_candidate("X Seen Film", 1976)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=movie.id,
                liked=True,
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert 'name="quick_rating" value="0"' not in page

    token = csrf_token_from(page)
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "0"},
        follow_redirects=True,
    )
    page = response.get_data(as_text=True)
    assert "The lowest rating" in page
    with app.app_context():
        assert (
            UserMovieStatus.query.filter_by(
                user_id=user_id, movie_id=movie_id, kind="not_interested"
            ).first()
            is None
        )

    # review_edit never offers the ✕ either.

    with app.app_context():
        review_id = (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).one().id
        )
    page = admin_client.get(f"/review/{review_id}/edit").get_data(as_text=True)
    assert 'name="quick_rating" value="0"' not in page


def test_same_day_retap_edits_todays_review(app, admin_client):
    """Make sure a second tap on the same day edits the review of that day.

    A different star on a day with a rating corrects that review in
    place. There is no second diary row and no rewatch. The liked flag
    follows the stars. After the day changes, the next tap is a new
    entry (rule set by Glenn)."""

    from datetime import datetime, timedelta

    with app.app_context():
        user_id = admin_id()
        movie = make_candidate("Same Day Rerate", 1986)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    token = csrf_token_from(page)
    admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "3"},
    )
    admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "4"},
    )
    with app.app_context():
        row = UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).one()
        assert float(row.rating) == 4.0
        assert row.liked is True
        assert row.rewatch is False

    # The user likes the film less in the evening. The correction can go
    # down too.

    admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "2"},
    )
    with app.app_context():
        row = UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).one()
        assert float(row.rating) == 2.0
        assert row.liked is False

        # Make the review 1 day older. The next tap is a new diary entry,
        # marked as a rewatch.

        row.date_reviewed = datetime.now() - timedelta(days=1)
        db.session.commit()

    admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "5"},
    )
    with app.app_context():
        rows = (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id)
            .order_by(UserMovieReview.id.asc())
            .all()
        )
        assert [float(row.rating) for row in rows] == [2.0, 5.0]
        assert rows[1].rewatch is True


def test_same_star_tap_removes_the_rating(app, admin_client):
    """Make sure a tap on the current rating removes it.

    A bare row with no date disappears fully. A dated viewing loses
    only its stars."""

    from app.videos import star_rating_fields

    with app.app_context():
        user_id = admin_id()
        bare_movie = make_candidate("Toggle Bare", 1984)
        dated_movie = make_candidate("Toggle Dated", 1985)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=dated_movie.id,
                liked=True,
                date_watched=__import__("datetime").datetime(2020, 5, 1),
                **star_rating_fields(3.0),
            )
        )
        db.session.commit()
        bare_id, dated_id = bare_movie.id, dated_movie.id

    page = admin_client.get(f"/movie/{bare_id}").get_data(as_text=True)
    token = csrf_token_from(page)
    admin_client.post(
        f"/movie/{bare_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "4"},
    )
    with app.app_context():
        assert (
            float(
                UserMovieReview.query.filter_by(user_id=user_id, movie_id=bare_id)
                .one()
                .rating
            )
            == 4.0
        )

    # The row now shows 4 filled stars and a remove hint.

    page = admin_client.get(f"/movie/{bare_id}").get_data(as_text=True)
    assert page.count("star-btn star filled") == 4
    assert "Tap again to remove your rating" in page

    admin_client.post(
        f"/movie/{bare_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "4"},
    )
    with app.app_context():
        assert (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=bare_id).first()
            is None
        )

    # A dated viewing keeps its history and loses only the stars.

    admin_client.post(
        f"/movie/{dated_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "3"},
    )
    with app.app_context():
        row = UserMovieReview.query.filter_by(user_id=user_id, movie_id=dated_id).one()
        assert row.rating is None
        assert row.liked is False
        assert row.date_watched is not None


def test_ladder_fetch_returns_state_without_redirect(app, admin_client):
    """Make sure a ladder fetch returns the state without a redirect.

    The background posts of the star row get JSON state back. Set,
    remove, flag, and unflag all complete without a redirect."""

    with app.app_context():
        movie = make_candidate("Fetch Film", 1986)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert 'data-ladder-live="1"' in page
    token = csrf_token_from(page)
    headers = {"X-Requested-With": "ladder"}

    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "5"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "rating": 5.0,
        "flagged": False,
        "estimated": None,
        "on_watchlist": False,
    }

    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "5"},
        headers=headers,
    )
    assert response.get_json() == {
        "rating": None,
        "flagged": False,
        "estimated": None,
        "on_watchlist": False,
    }

    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "0"},
        headers=headers,
    )
    assert response.get_json() == {
        "rating": None,
        "flagged": True,
        "estimated": None,
        "on_watchlist": False,
    }

    # The page renders the lit ✕ while the film is flagged. A second ✕
    # removes the flag.

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "x-btn active" in page
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "0"},
        headers=headers,
    )
    assert response.get_json() == {
        "rating": None,
        "flagged": False,
        "estimated": None,
        "on_watchlist": False,
    }


def test_half_star_ratings_render_a_partial_fill(app, admin_client):
    """Make sure a half-star rating renders a partial fill.

    Letterboxd logs in half-star steps. The widget fills part of the
    last star in the full gold. It does not round down to a whole star.
    A tap still submits only a whole value."""

    from app.videos import star_rating_fields

    with app.app_context():
        user_id = admin_id()
        movie = make_candidate("Half Star Film", 1975)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=movie.id,
                liked=True,
                **star_rating_fields(3.5),
            )
        )
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert page.count("star filled") == 4
    assert page.count("filled fill-partial") == 1
    assert "fill-50" in page
    assert "star estimated" not in page
