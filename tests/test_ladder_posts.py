"""Test the live star-row and watchlist posts (#335, #336, #337).

A live ladder post answers with the state of the star row. The state
carries has_review, so the ✕ hides after the first star tap (#335). A
rating on the watchlist page removes its tile (#336). The posts of 1
film run 1 at a time, on the server and in the page (#337)."""

from tests.conftest import ADMIN_EMAIL
from tests.factories import make_movie

LADDER = {"X-Requested-With": "ladder"}


def _movie(app, title, tmdb_id):
    from app import db

    with app.app_context():
        movie = make_movie(title, 1970, tmdb_id=tmdb_id)
        db.session.commit()
        return movie.id


def _token(client, movie_id):
    from tests.conftest import csrf_token_from

    return csrf_token_from(client.get(f"/movie/{movie_id}").get_data(as_text=True))


def test_live_ladder_state_carries_has_review(app, admin_client):
    movie_id = _movie(app, "Ladder Review", 880101)
    token = _token(admin_client, movie_id)

    # A ✕ on an unseen film: no diary row, so the ✕ stays

    state = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "quick_rating": "0"},
        headers=LADDER,
    ).get_json()
    assert state["flagged"] is True
    assert state["has_review"] is False

    # A star tap writes a diary row. The ✕ must hide

    state = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "quick_rating": "3"},
        headers=LADDER,
    ).get_json()
    assert state["rating"] == 3.0
    assert state["has_review"] is True


def test_star_posts_of_one_film_run_one_at_a_time(app, admin_client, monkeypatch):
    """Test that a quick-rating post holds the verdict lock of the film.

    The lock is taken before the post reads the latest row, and it goes
    only after the commit. Thus, a second tap sees the committed row and
    does not write a second row."""

    from app import db

    movie_id = _movie(app, "Ladder Lock", 880102)
    token = _token(admin_client, movie_id)

    events = []
    real_lock = app.redis.lock

    class RecordingLock:
        def __init__(self, name, **kwargs):
            self.name = name
            self.lock = real_lock(name, **kwargs)

        def acquire(self):
            events.append(("acquire", self.name))
            return self.lock.acquire()

        def release(self):
            events.append(("release", self.name))
            return self.lock.release()

    real_commit = db.session.commit

    def commit():
        events.append(("commit", None))
        return real_commit()

    monkeypatch.setattr(app.redis, "lock", RecordingLock)
    monkeypatch.setattr(db.session, "commit", commit)

    response = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "quick_rating": "4"},
        headers=LADDER,
    )
    assert response.status_code == 200

    kinds = [kind for kind, _ in events]
    assert kinds[0] == "acquire"
    assert events[0][1].endswith(f":movie:{movie_id}")
    assert "commit" in kinds
    assert kinds[-1] == "release"
    assert kinds.index("release") > max(
        i for i, kind in enumerate(kinds) if kind == "commit"
    )
    assert not app.redis.exists(events[0][1])

    with app.app_context():
        from app.models import User, UserMovieReview

        user = User.query.filter_by(email=ADMIN_EMAIL).one()
        assert (
            UserMovieReview.query.filter_by(user_id=user.id, movie_id=movie_id).count()
            == 1
        )


def test_page_script_guards_posts_and_clears_watchlist_tiles(app, admin_client):
    """Test the script of base.html for the in-flight guard and the tile
    removal.

    The page tests cannot run the script. Thus, this checks that the
    handlers have the guard and the shared removal."""

    body = admin_client.get("/").get_data(as_text=True)

    # Both live handlers post through backgroundPost, which ignores a tap
    # while the post of its form is in flight

    assert (
        "function backgroundPost(form, url, kind, data, onState, submitter) {" in body
    )
    assert body.count("if (form._postInFlight) return;") == 1
    assert (
        'backgroundPost(form, form.getAttribute("action") || window.location.href,'
        in body
    )
    assert 'backgroundPost(form, form.getAttribute("action"), "card", data,' in body

    # The fallback submission carries the tapped button (#406). Both
    # handlers pass it, and the fallback adds it as a hidden input before
    # form.submit()

    fallback = body[body.index("function backgroundPost(") :]
    fallback = fallback[: fallback.index("form.submit();")]
    assert "input.name = submitter.name;" in fallback
    assert "input.value = submitter.value;" in fallback
    assert "form.appendChild(input);" in fallback
    assert '}, {name: "quick_rating", value: button.value});' in body
    assert "}, submitter);" in body

    # Both handlers share afterTileState. It removes a watchlist-page tile
    # when the film left the watchlist. The removal counts down 1 time
    # only

    assert "function removeWatchlistCell(cell)" in body
    assert "cell._watchlistRemoved = true;" in body
    assert "if (state.on_watchlist === false) {" in body
    assert body.count("removeWatchlistCell(") == 2
    assert body.count("afterTileState(form, state);") == 2
