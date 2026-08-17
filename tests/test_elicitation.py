"""The rating drive at /rate: candidate pool exclusions, information-
value ranking, adjacency steering from the last response, and the four
response actions."""

import re

from app import db
from app.models import MovieCrew, UserMovieReview, UserWatchlist
from tests.factories import make_movie, make_movie_file
from tests.test_recommendations import (
    admin_id,
    genre,
    log_watch,
    make_cast,
    make_person,
)


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
        db.session.commit()

        mark_unseen(user_id, unseen.id)

        assert elicitation_candidates(user_id) == [eligible.id]


def test_unseen_mark_expires_after_two_years(app):
    """ "Haven't seen it" wears off (#52): a mark older than the
    resurface bar stops excluding the film from the drive, and
    re-marking a resurfaced film resets the clock on the same row for
    another term."""

    from datetime import datetime, timedelta

    from app.elicitation import (
        UNSEEN_RESURFACE_YEARS,
        elicitation_candidates,
        mark_unseen,
    )
    from app.models import UserMovieStatus

    with app.app_context():
        user_id = admin_id()
        film = make_candidate("Unseen Expiry", 1972)
        db.session.commit()
        film_id = film.id

        mark_unseen(user_id, film_id)
        assert film_id not in elicitation_candidates(user_id)

        # Age the mark past the bar: the film resurfaces

        row = UserMovieStatus.query.filter_by(
            user_id=user_id, movie_id=film_id, kind="unseen"
        ).one()
        row.date_added = datetime.now() - timedelta(
            days=UNSEEN_RESURFACE_YEARS * 365.25 + 30
        )
        db.session.commit()
        assert film_id in elicitation_candidates(user_id)

        # "Still haven't seen it" rests it for another term — one row,
        # fresh clock, excluded again

        mark_unseen(user_id, film_id)
        rows = UserMovieStatus.query.filter_by(
            user_id=user_id, movie_id=film_id, kind="unseen"
        ).all()
        assert len(rows) == 1
        assert rows[0].date_added > datetime.now() - timedelta(days=1)
        assert film_id not in elicitation_candidates(user_id)


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
    """The three answers (#62 removed Skip): a rating writes a
    date-less diary row and the film leaves the pool; watchlist and
    No Opinion both retire the film from the drive."""

    from app.elicitation import elicitation_candidates

    with app.app_context():
        user_id = admin_id()
        first = make_candidate("Drive First", 1980)
        second = make_candidate("Drive Second", 1981)
        third = make_candidate("Drive Third", 1982)
        db.session.commit()
        ids = (first.id, second.id, third.id)

    page = admin_client.get("/rate").get_data(as_text=True)
    assert "Rate Films" in page
    token = csrf_token_from(page)

    # Rate: an ordinary diary row, with no watch date, liked auto-set

    response = admin_client.post(
        "/rate",
        data={"csrf_token": token, "movie_id": str(ids[0]), "quick_rating": "4"},
    )
    assert response.status_code == 302
    with app.app_context():
        review = UserMovieReview.query.filter_by(user_id=user_id, movie_id=ids[0]).one()
        assert float(review.rating) == 4.0
        assert review.liked is True
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

    # No Opinion retires the film too

    admin_client.post(
        "/rate",
        data={
            "csrf_token": token,
            "movie_id": str(ids[2]),
            "unseen_submit": "No Opinion",
        },
    )
    with app.app_context():
        assert elicitation_candidates(user_id) == []

    page = admin_client.get("/rate").get_data(as_text=True)
    assert "Nothing left to offer right now." in page


def test_quick_answer_buttons_map_to_whole_stars(app, admin_client):
    """The one-tap ladder writes ordinary date-less diary rows — Loved
    it = 5 (positive, steers), Didn't like it = 2 (not positive) —
    while ✕ (0) writes the not-interested FLAG, never a review (#51),
    and nonsense values write nothing."""

    from app.elicitation import elicitation_candidates, last_response

    with app.app_context():
        user_id = admin_id()
        loved = make_candidate("Quick Loved", 1980)
        shunned = make_candidate("Quick Shunned", 1981)
        disliked = make_candidate("Quick Disliked", 1982)
        untouched = make_candidate("Quick Untouched", 1983)
        db.session.commit()
        loved_id, shunned_id = loved.id, shunned.id
        disliked_id, untouched_id = disliked.id, untouched.id

    page = admin_client.get("/rate").get_data(as_text=True)
    token = csrf_token_from(page)
    for label in (
        "Not interested",
        "Hated it",
        "Didn&#39;t like it",
        "Liked it",
        "Really liked it",
        "Loved it",
    ):
        assert label in page

    # The 1–5 buttons are single star glyphs in one row, labels in titles

    assert page.count("&#9733;") == 5

    response = admin_client.post(
        "/rate",
        data={"csrf_token": token, "movie_id": str(loved_id), "quick_rating": "5"},
    )
    assert response.status_code == 302
    with app.app_context():
        review = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=loved_id
        ).one()
        assert float(review.rating) == 5.0
        assert review.liked is True
        assert review.date_watched is None
        assert last_response(app.redis, user_id)["positive"] is True

    admin_client.post(
        "/rate",
        data={"csrf_token": token, "movie_id": str(shunned_id), "quick_rating": "0"},
    )
    with app.app_context():
        from app.models import UserMovieStatus

        assert (
            UserMovieReview.query.filter_by(
                user_id=user_id, movie_id=shunned_id
            ).first()
            is None
        )
        flag = UserMovieStatus.query.filter_by(
            user_id=user_id, movie_id=shunned_id, kind="not_interested"
        ).one()
        assert flag is not None
        last = last_response(app.redis, user_id)
        assert last["action"] == "not_interested"
        assert last["positive"] is False
        assert shunned_id not in elicitation_candidates(user_id)

    admin_client.post(
        "/rate",
        data={"csrf_token": token, "movie_id": str(disliked_id), "quick_rating": "2"},
    )
    with app.app_context():
        review = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=disliked_id
        ).one()
        assert float(review.rating) == 2.0
        assert review.liked is False

    # A nonsense value writes nothing

    admin_client.post(
        "/rate",
        data={"csrf_token": token, "movie_id": str(untouched_id), "quick_rating": "7"},
    )
    with app.app_context():
        assert (
            UserMovieReview.query.filter_by(
                user_id=user_id, movie_id=untouched_id
            ).first()
            is None
        )


def test_movie_page_ladder_logs_a_quick_rating(app, admin_client):
    """The ladder rides the movie page's log form too: one tap logs a
    rating through the standard review path, honoring the form's other
    fields as submitted."""

    with app.app_context():
        user_id = admin_id()
        movie = make_candidate("Ladder Movie Page", 1970)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert page.count("&#9733;") == 5
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
    """A 3+ ladder tap on the movie page earns the "since you liked"
    strip on the redirect back to that page — and only that page; a
    banked suggestion joins the watchlist without moving the anchor."""

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

    # The strip belongs to the anchor's page alone

    other = admin_client.get(f"/movie/{unrelated_id}").get_data(as_text=True)
    assert "Since you liked" not in other

    # Banking a suggestion adds it to the watchlist and leaves the
    # anchor (and the steering) untouched

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

    # First GET renders the one-shot flash (which names the film); the
    # second shows the steady state: the banked film left the strip,
    # its sibling remains

    admin_client.get(f"/movie/{anchor_id}")
    page = admin_client.get(f"/movie/{anchor_id}").get_data(as_text=True)
    assert "Page Strip Similar (1961)" not in page
    assert "Page Strip Similar Two (1962)" in page

    # Rating a suggestion from the strip rates THAT film, date-less,
    # without moving the anchor — the strip refreshes minus the film

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


def test_rate_suggestions_are_rateable_and_reanchor(app, admin_client):
    """On the drive, a ladder tap on a suggestion card rates that film
    and RE-ANCHORS the session to it — the chain continues from the
    film just rated."""

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

    page = admin_client.get("/rate").get_data(as_text=True)
    token = csrf_token_from(page)
    admin_client.post(
        "/rate",
        data={"csrf_token": token, "movie_id": str(anchor_id), "quick_rating": "4"},
    )

    strip = admin_client.get("/rate").get_data(as_text=True)
    assert "Chain Similar (1961)" in strip

    admin_client.post(
        "/rate",
        data={"csrf_token": token, "movie_id": str(similar_id), "quick_rating": "5"},
    )
    with app.app_context():
        row = UserMovieReview.query.filter_by(
            user_id=user_id, movie_id=similar_id
        ).one()
        assert float(row.rating) == 5.0
        state = last_response(app.redis, user_id)
        assert state["movie_id"] == similar_id
        assert state["positive"] is True


def test_rate_page_shows_featured_details_only(app, admin_client):
    """One card at a time — what's next stays a mystery, the carrot
    for answering."""

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
        lead = make_person(777104, "Leading Lady")
        second = make_person(777105, "Second Banana")
        make_cast(second, featured, character="The Pal", order=1)
        make_cast(lead, featured, character="The Lead", order=0)
        # Lower-information companions must stay hidden
        for n in range(3):
            make_candidate(f"Drive Filler {n}", 1990)
        db.session.commit()

    page = admin_client.get("/rate").get_data(as_text=True)
    assert "Drive Featured (1956)" in page
    assert "119 min" in page
    assert "Western" in page
    assert "A searcher searches." in page

    # Director and cast names link to their filmography pages, cast in
    # billing order under the synopsis

    assert (
        'Directed by <a href="/library/movie?credit=777004" '
        'class="link-secondary text-secondary">Featured Director</a>' in page
    )
    assert (
        'Starring <a href="/library/movie?credit=777104" '
        'class="link-secondary text-secondary">Leading Lady</a>, '
        '<a href="/library/movie?credit=777105" '
        'class="link-secondary text-secondary">Second Banana</a>' in page
    )
    assert "Up next" not in page
    assert "Drive Filler" not in page


def test_positive_rating_earns_suggestions(app, admin_client):
    """A ≥3.5 rating surfaces up to three taste-adjacent unseen films —
    bankable to the watchlist without moving the drive along — while a
    sour rating earns nothing."""

    import json

    from app.elicitation import last_response

    with app.app_context():
        user_id = admin_id()
        western = genre(37, "Western")
        auteur = make_person(777005, "Suggestion Director")
        anchor = make_candidate(
            "Suggest Anchor", 1960, genre_row=western, director=auteur
        )
        similar = make_candidate(
            "Suggest Similar", 1961, genre_row=western, director=auteur
        )
        # An unrelated candidate that shares nothing with the anchor
        # (different genre AND decade) can never join the strip
        drama = genre(18, "Drama")
        make_candidate("Suggest Unrelated", 1999, genre_row=drama)
        db.session.commit()
        anchor_id, similar_id = anchor.id, similar.id

    # A taste profile so score_movie has something to work with

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

    page = admin_client.get("/rate").get_data(as_text=True)
    token = csrf_token_from(page)

    response = admin_client.post(
        "/rate",
        data={"csrf_token": token, "movie_id": str(anchor_id), "quick_rating": "4"},
    )
    assert response.status_code == 302

    page = admin_client.get("/rate").get_data(as_text=True)
    assert "Since you liked Suggest Anchor" in page
    assert "Suggest Similar (1961)" in page
    strip = page[page.index("Since you liked") :]
    assert "Suggest Unrelated" not in strip

    # Banking the suggestion adds it to the watchlist WITHOUT moving
    # the steering — the strip stays anchored, minus the banked film

    response = admin_client.post(
        "/rate",
        data={
            "csrf_token": token,
            "movie_id": str(similar_id),
            "want_suggestion_submit": "Add to Watchlist",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        assert (
            UserWatchlist.query.filter_by(user_id=user_id, movie_id=similar_id).first()
            is not None
        )
        assert last_response(app.redis, user_id)["movie_id"] == anchor_id

    # The first GET renders the one-shot flash (which names the film);
    # the second shows the page's own steady state

    admin_client.get("/rate")
    page = admin_client.get("/rate").get_data(as_text=True)
    assert "Suggest Similar (1961)" not in page

    # A sour rating earns no strip

    with app.app_context():
        sour = make_candidate("Suggest Sour", 1962, genre_row=genre(37, "Western"))
        db.session.commit()
        sour_id = sour.id
    admin_client.post(
        "/rate",
        data={"csrf_token": token, "movie_id": str(sour_id), "quick_rating": "2"},
    )
    page = admin_client.get("/rate").get_data(as_text=True)
    assert "Since you liked" not in page


def test_movie_page_x_flags_and_never_reviews(app, admin_client):
    """The movie page ladder's ✕ writes the not-interested flag — no
    diary row — and clears a contradicting watchlist entry; logging the
    film later clears the flag again (#51)."""

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

    # Watching it anyway contradicts the flag, which clears with the log

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
    """A film with a diary row refuses the ✕ server-side — its floor is
    1 star — and the ladder stops offering the button (#51)."""

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
    assert "the lowest rating" in page
    with app.app_context():
        assert (
            UserMovieStatus.query.filter_by(
                user_id=user_id, movie_id=movie_id, kind="not_interested"
            ).first()
            is None
        )

    # review_edit never offers the ✕ either

    with app.app_context():
        review_id = (
            UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).one().id
        )
    page = admin_client.get(f"/review/{review_id}/edit").get_data(as_text=True)
    assert 'name="quick_rating" value="0"' not in page


def test_same_day_retap_edits_todays_review(app, admin_client):
    """A different star on a day you already rated corrects that
    review in place — no second diary row, no rewatch, liked follows
    the stars — while the day rolling over makes the next tap a fresh
    entry (Glenn's rule)."""

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

    # Taste soured by evening: the correction can go down too

    admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "2"},
    )
    with app.app_context():
        row = UserMovieReview.query.filter_by(user_id=user_id, movie_id=movie_id).one()
        assert float(row.rating) == 2.0
        assert row.liked is False

        # Age the review a day: the next tap is a fresh diary entry,
        # marked as a rewatch

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
    """Tapping your current rating removes it (#54): a bare date-less
    row disappears entirely, a dated viewing only loses its stars."""

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

    # The row now shows four filled stars and a remove hint

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

    # A dated viewing keeps its history and loses only the stars

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
    """The star row's background posts get JSON state back (#54): set,
    remove, flag, and unflag all round-trip without a redirect."""

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
    assert response.get_json() == {"rating": 5.0, "flagged": False, "estimated": None}

    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "5"},
        headers=headers,
    )
    assert response.get_json() == {"rating": None, "flagged": False, "estimated": None}

    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "0"},
        headers=headers,
    )
    assert response.get_json() == {"rating": None, "flagged": True, "estimated": None}

    # The page renders the lit ✕ while flagged; a second ✕ undoes it

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "x-btn active" in page
    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "review_submit": "y", "quick_rating": "0"},
        headers=headers,
    )
    assert response.get_json() == {"rating": None, "flagged": False, "estimated": None}


def test_rate_featured_card_shows_the_estimate(app, admin_client):
    """The featured card's star row previews the engine's estimate from
    the nightly score map (#53 — Glenn chose consistency over keeping
    the elicitation unanchored)."""

    import json as jsonlib

    from app.recommendations import PROFILE_KEY, SCORES_KEY

    with app.app_context():
        user_id = admin_id()
        featured = make_candidate("Estimate Featured", 1990)
        db.session.commit()
        featured_id = featured.id

    app.redis.set(
        SCORES_KEY.format(user_id=user_id), jsonlib.dumps({str(featured_id): 9.0})
    )
    app.redis.set(
        PROFILE_KEY.format(user_id=user_id),
        jsonlib.dumps(
            {
                "affinities": {},
                "movies": 3,
                "calibration": {
                    "scores": [0.0, 1.0, 2.0, 3.0],
                    "stars": [1.0, 2.0, 4.0, 4.5],
                },
            }
        ),
    )

    page = admin_client.get("/rate").get_data(as_text=True)

    # The 4.5 estimate fills four stars and half of the fifth

    assert page.count("star estimated") == 5
    assert page.count("estimated est-partial") == 1
    assert "--est-fill: 50%" in page
    assert "Estimated 4.5 for you" in page


def test_half_star_ratings_render_a_partial_fill(app, admin_client):
    """Letterboxd logs in half-star increments; the widget part-fills
    the last star in the full gold instead of flooring to whole. Taps
    still submit whole values only."""

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
    assert "--fill: 50%" in page
    assert "star estimated" not in page
