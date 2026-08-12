"""The rating drive at /rate: candidate pool exclusions, information-
value ranking, adjacency steering from the last response, and the four
response actions."""

import re

from app import db
from app.models import MovieCrew, UserMovieReview, UserWatchlist
from tests.factories import make_movie, make_movie_file
from tests.test_recommendations import admin_id, genre, log_watch, make_person


def csrf_token_from(page_html):
    """The CSRF token baked into a rendered form."""

    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def make_candidate(title, year, genre_row=None, director=None):
    """A library film eligible for the drive, with optional features."""

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
    from app.elicitation import (
        elicitation_candidates,
        mark_skipped,
        mark_unseen,
    )

    with app.app_context():
        user_id = admin_id()
        eligible = make_candidate("Elicit Eligible", 1990)
        logged = make_candidate("Elicit Logged", 1991)
        log_watch(user_id, logged, rating=4)
        wanted = make_candidate("Elicit Wanted", 1992)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))
        unseen = make_candidate("Elicit Unseen", 1993)
        skipped = make_candidate("Elicit Skipped", 1994)
        db.session.commit()

        mark_unseen(app.redis, user_id, unseen.id)
        mark_skipped(app.redis, user_id, skipped.id)

        assert elicitation_candidates(user_id) == [eligible.id]


def test_information_scores_prefer_unrated_reach(app):
    """Films whose features are well represented in the library but
    thin in the diary outrank films the profile already knows."""

    from app.elicitation import information_scores
    from app.recommendations import collect_features

    with app.app_context():
        comedy = genre(35, "Comedy")
        drama = genre(18, "Drama")
        auteur = make_person(777001, "Unrated Auteur")

        known = make_candidate("Info Known Comedy", 1990, genre_row=comedy)
        fresh = [
            make_candidate(
                f"Info Fresh Drama {n}", 1990, genre_row=drama, director=auteur
            )
            for n in range(3)
        ]
        db.session.commit()

        candidates = [known.id] + [movie.id for movie in fresh]
        features = collect_features(candidates)
        profile = {"affinities": {"genre:35": {"count": 3, "score": 0.5}}}
        scores = information_scores(candidates, features, profile)

        assert all(scores[movie.id] > scores[known.id] for movie in fresh)


def test_adjacency_steers_toward_the_rated_films_neighborhood(app):
    """After rating a film, its director's other film jumps the queue."""

    from app.elicitation import next_films, set_last_response

    with app.app_context():
        user_id = admin_id()
        western = genre(37, "Western")
        shared = make_person(777002, "Anchor Director")
        other = make_person(777003, "Other Director")

        anchor = make_candidate(
            "Adjacent Anchor", 1960, genre_row=western, director=shared
        )
        log_watch(user_id, anchor, rating=5)

        similar = make_candidate(
            "Adjacent Similar", 1961, genre_row=western, director=shared
        )
        unrelated = make_candidate(
            "Adjacent Unrelated", 1962, genre_row=western, director=other
        )
        db.session.commit()

        set_last_response(app.redis, user_id, anchor.id, "rated")
        queue = next_films(user_id, count=2)
        assert queue == [similar.id, unrelated.id]


def test_rate_page_actions_flow(app, admin_client):
    """The four answers: a rating writes a date-less diary row and the
    film leaves the pool; watchlist, haven't-seen, and skip all retire
    the film from the drive."""

    from app.elicitation import elicitation_candidates

    with app.app_context():
        user_id = admin_id()
        first = make_candidate("Drive First", 1980)
        second = make_candidate("Drive Second", 1981)
        third = make_candidate("Drive Third", 1982)
        fourth = make_candidate("Drive Fourth", 1983)
        db.session.commit()
        ids = (first.id, second.id, third.id, fourth.id)

    page = admin_client.get("/rate").get_data(as_text=True)
    assert "Rate Films" in page
    token = csrf_token_from(page)

    # Rate: an ordinary diary row, with no watch date

    response = admin_client.post(
        "/rate",
        data={
            "csrf_token": token,
            "movie_id": str(ids[0]),
            "rating": "4.5",
            "rate_submit": "Rate It",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        review = UserMovieReview.query.filter_by(user_id=user_id, movie_id=ids[0]).one()
        assert float(review.rating) == 4.5
        assert review.date_watched is None
        assert review.date_reviewed is not None
        assert ids[0] not in elicitation_candidates(user_id)

    # Watchlist: files the film as unseen-but-wanted

    admin_client.post(
        "/rate",
        data={
            "csrf_token": token,
            "movie_id": str(ids[1]),
            "watchlist_submit": "Add to Watchlist",
        },
    )
    with app.app_context():
        assert (
            UserWatchlist.query.filter_by(user_id=user_id, movie_id=ids[1]).first()
            is not None
        )
        assert ids[1] not in elicitation_candidates(user_id)

    # Haven't seen and skip both retire the film

    admin_client.post(
        "/rate",
        data={
            "csrf_token": token,
            "movie_id": str(ids[2]),
            "unseen_submit": "Haven't Seen It",
        },
    )
    admin_client.post(
        "/rate",
        data={"csrf_token": token, "movie_id": str(ids[3]), "skip_submit": "Skip"},
    )
    with app.app_context():
        assert elicitation_candidates(user_id) == []

    page = admin_client.get("/rate").get_data(as_text=True)
    assert "Nothing left to offer right now." in page


def test_rate_page_shows_featured_details_and_up_next(app, admin_client):
    with app.app_context():
        western = genre(37, "Western")
        director = make_person(777004, "Featured Director")
        featured = make_movie(
            "Drive Featured",
            1956,
            tmdb_runtime=119,
            tmdb_overview="A searcher searches.",
        )
        make_movie_file(featured, "Bluray-1080p")
        featured.genres.append(western)
        db.session.add(
            MovieCrew(
                movie_id=featured.id,
                credit_id=director.id,
                department="Directing",
                job="Director",
            )
        )
        # Lower-information companions fill the up-next strip
        for n in range(3):
            make_candidate(f"Drive Filler {n}", 1990)
        db.session.commit()

    page = admin_client.get("/rate").get_data(as_text=True)
    assert "Drive Featured (1956)" in page
    assert "Directed by Featured Director" in page
    assert "119 min" in page
    assert "Western" in page
    assert "A searcher searches." in page
    assert "Up next" in page
    assert "Drive Filler" in page
