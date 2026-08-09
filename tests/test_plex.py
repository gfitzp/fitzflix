"""Direct Plex watch tracking: the webhook endpoint, the history poller,
and the shared apply task that feeds household shopping-cart priority and
mapped users' diaries.
"""

import json
import re
import time

from datetime import datetime

import pytest

from app import db
from app.models import Movie, User, UserMovieReview
from app.videos import apply_plex_watch, plex_history_poll
from tests.conftest import ADMIN_EMAIL
from tests.factories import make_movie

WEBHOOK = "/api/plex/webhook/plex-test-webhook-token"


def scrobble_payload(
    tmdb_id=578,
    username="glenn-plex",
    event="media.scrobble",
    media_type="movie",
    guid_style="new",
):
    metadata = {"type": media_type, "title": "Jaws"}
    if guid_style == "new":
        metadata["Guid"] = [
            {"id": "imdb://tt0073195"},
            {"id": f"tmdb://{tmdb_id}"},
        ]
    else:
        metadata["guid"] = f"com.plexapp.agents.themoviedb://{tmdb_id}?lang=en"
    return {
        "event": event,
        "Account": {"id": 1, "title": username},
        "Metadata": metadata,
    }


def post_webhook(client, payload, url=WEBHOOK):
    # Plex posts multipart form data with the JSON in a "payload" field
    return client.post(
        url,
        data={"payload": json.dumps(payload)},
        content_type="multipart/form-data",
    )


def plex_jobs(app):
    return [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.apply_plex_watch"
    ]


@pytest.fixture
def mapped_admin(app):
    """The admin user temporarily mapped to the 'glenn-plex' Plex account.

    The user table survives between tests, so the mapping is undone after.
    """

    with app.app_context():
        user = User.query.filter_by(email=ADMIN_EMAIL).one()
        user.plex_username = "glenn-plex"
        db.session.commit()
        user_id = user.id
    yield user_id
    with app.app_context():
        db.session.get(User, user_id).plex_username = None
        db.session.commit()


def test_webhook_scrobble_enqueues_apply_task(app, client):
    response = post_webhook(client, scrobble_payload())
    assert response.status_code == 204

    jobs = plex_jobs(app)
    assert len(jobs) == 1
    tmdb_id, username, viewed_at, source = jobs[0].args
    assert (tmdb_id, username, source) == (578, "glenn-plex", "webhook")
    assert datetime.fromisoformat(viewed_at).tzinfo is not None


def test_webhook_parses_legacy_guid(app, client):
    response = post_webhook(client, scrobble_payload(guid_style="legacy"))
    assert response.status_code == 204
    assert plex_jobs(app)[0].args[0] == 578


def test_webhook_rejects_bad_token(app, client):
    response = post_webhook(
        client, scrobble_payload(), url="/api/plex/webhook/wrong-token"
    )
    assert response.status_code == 404
    assert plex_jobs(app) == []


def test_webhook_ignores_non_scrobbles_and_tv(app, client):
    assert post_webhook(client, scrobble_payload(event="media.play")).status_code == 204
    assert (
        post_webhook(client, scrobble_payload(media_type="episode")).status_code == 204
    )
    assert plex_jobs(app) == []


def test_apply_watch_increments_and_records_diary(app, mapped_admin):
    with app.app_context():
        movie = make_movie("Watched on Plex", 1975, tmdb_id=578)
        db.session.commit()
        movie_id = movie.id

        first_watch = "2026-08-01T21:15:00+00:00"
        assert apply_plex_watch(578, "glenn-plex", first_watch, "webhook") is True

        # The task ran in its own app context and session; drop this
        # session's cached state so the asserts read fresh rows

        db.session.expire_all()
        movie = db.session.get(Movie, movie_id)
        assert movie.shopping_cart_priority == 1
        assert movie.shopping_cart_add_date is not None

        diary = UserMovieReview.query.filter_by(
            user_id=mapped_admin, movie_id=movie_id
        ).all()
        assert len(diary) == 1
        assert diary[0].date_watched == datetime(2026, 8, 1)
        assert diary[0].rating is None
        assert diary[0].liked is False
        assert diary[0].rewatch is False

        # The same watch reported again (the poller catching up after the
        # webhook already recorded it) is a no-op

        assert apply_plex_watch(578, "glenn-plex", first_watch, "history") is True
        db.session.expire_all()
        assert db.session.get(Movie, movie_id).shopping_cart_priority == 1
        assert (
            UserMovieReview.query.filter_by(
                user_id=mapped_admin, movie_id=movie_id
            ).count()
            == 1
        )

        # A watch on a later date is a rewatch

        assert (
            apply_plex_watch(578, "glenn-plex", "2026-08-05T20:00:00+00:00", "webhook")
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, movie_id).shopping_cart_priority == 2
        rewatch_row = UserMovieReview.query.filter_by(
            user_id=mapped_admin, movie_id=movie_id, date_watched=datetime(2026, 8, 5)
        ).one()
        assert rewatch_row.rewatch is True


def test_apply_watch_from_unmapped_account_only_increments(app):
    with app.app_context():
        movie = make_movie("Household Film", 1980, tmdb_id=91234)
        db.session.commit()
        movie_id = movie.id

        assert (
            apply_plex_watch(
                91234, "houseguest", "2026-08-02T19:00:00+00:00", "history"
            )
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, movie_id).shopping_cart_priority == 1
        assert UserMovieReview.query.count() == 0


def test_apply_watch_of_unknown_movie_is_ignored(app):
    with app.app_context():
        assert (
            apply_plex_watch(
                999999, "glenn-plex", "2026-08-02T19:00:00+00:00", "webhook"
            )
            is True
        )
        assert UserMovieReview.query.count() == 0


class FakePlexServer:
    """Canned responses for the poller's three Plex endpoints."""

    def __init__(self, history=None, accounts=None, guids=None):
        self.history = history or []
        self.accounts = accounts or []
        self.guids = guids or {}
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(url)

        class Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        if "/status/sessions/history/all" in url:
            return Response({"MediaContainer": {"Metadata": self.history}})
        if "/accounts" in url:
            return Response({"MediaContainer": {"Account": self.accounts}})
        match = re.search(r"/library/metadata/(\w+)", url)
        if match:
            guid = self.guids.get(match.group(1))
            metadata = [{"Guid": [{"id": guid}]}] if guid else []
            return Response({"MediaContainer": {"Metadata": metadata}})
        raise AssertionError(f"unexpected Plex request: {url}")


@pytest.fixture
def plex_server(app, monkeypatch):
    import app.videos as videos

    server = FakePlexServer()
    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test:32400")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "plex-test-token")
    monkeypatch.setattr(videos.requests, "get", server.get)
    return server


def test_poller_first_run_only_plants_the_cursor(app, plex_server):
    with app.app_context():
        assert plex_history_poll() is True
    assert app.redis.get("fitzflix:plex:history-cursor") is not None
    assert plex_server.requests == []
    assert plex_jobs(app) == []


def test_poller_enqueues_new_movie_watches_and_advances_cursor(app, plex_server):
    cursor = int(time.time()) - 3600
    app.redis.set("fitzflix:plex:history-cursor", cursor)

    plex_server.history = [
        # An old entry at the cursor is already processed
        {"type": "movie", "viewedAt": cursor, "accountID": 1, "ratingKey": "10"},
        {"type": "movie", "viewedAt": cursor + 100, "accountID": 1, "ratingKey": "42"},
        # Episodes advance the cursor but aren't recorded
        {
            "type": "episode",
            "viewedAt": cursor + 200,
            "accountID": 2,
            "ratingKey": "77",
        },
        {"type": "movie", "viewedAt": cursor + 300, "accountID": 2, "ratingKey": "43"},
    ]
    plex_server.accounts = [
        {"id": 1, "name": "glenn-plex"},
        {"id": 2, "name": "monica-plex"},
    ]
    plex_server.guids = {"42": "tmdb://578", "43": "tmdb://11"}

    with app.app_context():
        assert plex_history_poll() is True

    jobs = plex_jobs(app)
    assert [(job.args[0], job.args[1], job.args[3]) for job in jobs] == [
        (578, "glenn-plex", "history"),
        (11, "monica-plex", "history"),
    ]
    assert int(app.redis.get("fitzflix:plex:history-cursor")) == cursor + 300


def test_poller_caches_guid_lookups(app, plex_server):
    cursor = int(time.time()) - 3600
    app.redis.set("fitzflix:plex:history-cursor", cursor)
    plex_server.history = [
        {"type": "movie", "viewedAt": cursor + 100, "accountID": 1, "ratingKey": "42"}
    ]
    plex_server.accounts = [{"id": 1, "name": "glenn-plex"}]
    plex_server.guids = {"42": "tmdb://578"}

    with app.app_context():
        assert plex_history_poll() is True
        metadata_fetches = [
            r for r in plex_server.requests if "/library/metadata/" in r
        ]
        assert len(metadata_fetches) == 1

        # A later poll seeing the same rating key skips the metadata fetch

        app.redis.set("fitzflix:plex:history-cursor", cursor)
        assert plex_history_poll() is True
        metadata_fetches = [
            r for r in plex_server.requests if "/library/metadata/" in r
        ]
        assert len(metadata_fetches) == 1


def test_profile_maps_and_unmaps_plex_username(app, admin_client):
    page = admin_client.get("/profile").get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    response = admin_client.post(
        "/profile",
        data={
            "csrf_token": token,
            "plex_username": "glenn-plex",
            "plex_submit": "Update Plex Mapping",
        },
        follow_redirects=True,
    )
    assert "now count as yours" in response.get_data(as_text=True)
    with app.app_context():
        assert (
            User.query.filter_by(email=ADMIN_EMAIL).one().plex_username == "glenn-plex"
        )

    # A second user can't claim the same Plex account

    from tests.conftest import MEMBER_EMAIL, _signed_in_client

    member_client = _signed_in_client(app, MEMBER_EMAIL)
    member_page = member_client.get("/profile").get_data(as_text=True)
    member_token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', member_page
    ).group(1)
    response = member_client.post(
        "/profile",
        data={
            "csrf_token": member_token,
            "plex_username": "glenn-plex",
            "plex_submit": "Update Plex Mapping",
        },
        follow_redirects=True,
    )
    assert "already mapped to another user" in response.get_data(as_text=True)

    # Blank unmaps

    response = admin_client.post(
        "/profile",
        data={
            "csrf_token": token,
            "plex_username": "",
            "plex_submit": "Update Plex Mapping",
        },
        follow_redirects=True,
    )
    assert "Removed your Plex username mapping" in response.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(email=ADMIN_EMAIL).one().plex_username is None


def test_reviews_page_badges_rewatches(app, admin_client):
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Seen Twice", 1999)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=movie.id,
                review="",
                date_watched=datetime(2026, 8, 5),
                rewatch=True,
                **star_rating_fields(None),
            )
        )
        db.session.commit()

    page = admin_client.get("/history").get_data(as_text=True)
    assert "Rewatch" in page
