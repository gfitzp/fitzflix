"""Test direct Plex watch tracking.

The tests cover the webhook endpoint, the history poller, and the shared
apply task. That task feeds the household shopping-cart priority and
the diaries of the mapped users.
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
    # Plex posts multipart form data with the JSON in a "payload" field.
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
    """Map the admin user to the 'glenn-plex' Plex account for 1 test.

    The user table survives between tests. Thus, the fixture removes the
    mapping after the test.
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


def test_webhook_rejects_a_non_ascii_token(app, client):
    response = post_webhook(client, scrobble_payload(), url="/api/plex/webhook/%C3%A9")
    assert response.status_code == 404
    assert plex_jobs(app) == []


def test_webhook_ignores_non_scrobbles_and_tv(app, client):
    assert post_webhook(client, scrobble_payload(event="media.play")).status_code == 204
    assert (
        post_webhook(client, scrobble_payload(media_type="episode")).status_code == 204
    )
    assert plex_jobs(app) == []


def test_webhook_ignores_live_tv_scrobbles(app, client):
    """Make sure the webhook ignores a live TV scrobble.

    A virtual-channel airing (#182) has the movie type and can carry a
    matched TMDB guid. But a channel surf is never a diary watch."""

    payload = scrobble_payload()
    payload["Metadata"]["live"] = "1"
    assert post_webhook(client, payload).status_code == 204
    assert plex_jobs(app) == []


def test_a_failed_watch_write_lets_the_other_source_record_it(
    app, mapped_admin, monkeypatch
):
    """A failed commit releases the dedup marker. The poller then records it."""

    import app.diary as diary

    with app.app_context():
        movie = make_movie("Failed Write", 1976, tmdb_id=579)
        db.session.commit()
        movie_id = movie.id

    watched = "2026-08-02T21:15:00+00:00"
    real_commit = db.session.commit
    calls = []

    def failing_commit():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database restarting")
        return real_commit()

    with app.app_context():
        monkeypatch.setattr(diary.db.session, "commit", failing_commit)
        assert apply_plex_watch(579, "glenn-plex", watched, "webhook") is False
        monkeypatch.undo()
        assert apply_plex_watch(579, "glenn-plex", watched, "history") is True
        db.session.expire_all()
        assert (
            UserMovieReview.query.filter_by(
                user_id=mapped_admin, movie_id=movie_id
            ).count()
            == 1
        )


def test_apply_watch_increments_and_records_diary(app, mapped_admin):
    with app.app_context():
        movie = make_movie("Watched on Plex", 1975, tmdb_id=578)
        db.session.commit()
        movie_id = movie.id

        first_watch = "2026-08-01T21:15:00+00:00"
        assert apply_plex_watch(578, "glenn-plex", first_watch, "webhook") is True

        # The task ran in its own app context and session. Delete the
        # cached state of this session. Then the asserts read new rows.

        db.session.expire_all()
        movie = db.session.get(Movie, movie_id)
        assert movie.shopping_cart_priority == 1
        assert movie.shopping_cart_add_date is not None

        diary = UserMovieReview.query.filter_by(
            user_id=mapped_admin, movie_id=movie_id
        ).all()
        assert len(diary) == 1
        # Fitzflix stores the full watch timestamp in local wall-clock
        # time. It is not UTC, and it is not a date cut to midnight.
        expected = datetime.fromisoformat(first_watch).astimezone().replace(tzinfo=None)
        assert diary[0].date_watched == expected
        assert diary[0].rating is None
        assert diary[0].liked is False
        assert diary[0].rewatch is False

        # The same watch reported again is a no-op. This occurs when the
        # poller catches up after the webhook already recorded the watch.

        assert apply_plex_watch(578, "glenn-plex", first_watch, "history") is True
        db.session.expire_all()
        assert db.session.get(Movie, movie_id).shopping_cart_priority == 1
        assert (
            UserMovieReview.query.filter_by(
                user_id=mapped_admin, movie_id=movie_id
            ).count()
            == 1
        )

        # A watch on a later date is a rewatch.

        assert (
            apply_plex_watch(578, "glenn-plex", "2026-08-05T20:00:00+00:00", "webhook")
            is True
        )
        db.session.expire_all()
        assert db.session.get(Movie, movie_id).shopping_cart_priority == 2
        expected = (
            datetime.fromisoformat("2026-08-05T20:00:00+00:00")
            .astimezone()
            .replace(tzinfo=None)
        )
        rewatch_row = UserMovieReview.query.filter_by(
            user_id=mapped_admin, movie_id=movie_id, date_watched=expected
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
    """Give canned responses for the 3 Plex endpoints of the poller.

    The history answer honours the viewedAt> filter of the query, as a
    real server does. Then a poll that asks for too late a start loses
    the entries before it (#425)."""

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
            params = kwargs.get("params") or {}
            history = self.history
            if "viewedAt>" in params:
                after = int(params["viewedAt>"])
                history = [e for e in history if int(e.get("viewedAt", 0)) > after]
            return Response({"MediaContainer": {"Metadata": history}})
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
    from app import plex_api

    server = FakePlexServer()
    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test:32400")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "plex-test-token")
    monkeypatch.setattr(plex_api.requests, "get", server.get)
    return server


def test_a_failed_lookup_holds_the_cursor_for_the_next_poll(
    app, plex_server, monkeypatch
):
    """A timeout on the metadata lookup must not lose the watch."""

    cursor = int(time.time()) - 3600
    app.redis.set("fitzflix:plex:history-cursor", cursor)
    plex_server.history = [
        {"type": "movie", "viewedAt": cursor + 100, "accountID": 1, "ratingKey": "50"},
        {"type": "movie", "viewedAt": cursor + 200, "accountID": 1, "ratingKey": "51"},
    ]
    plex_server.accounts = [{"id": 1, "name": "glenn-plex"}]
    plex_server.guids = {"51": "tmdb://12"}
    real_get = plex_server.get

    def flaky_get(url, *args, **kwargs):
        if url.endswith("/library/metadata/50"):
            raise TimeoutError("Plex is busy")
        return real_get(url, *args, **kwargs)

    from app import plex_api

    monkeypatch.setattr(plex_api.requests, "get", flaky_get)

    with app.app_context():
        assert plex_history_poll() is True

    # The cursor stays before the failed entry. The next poll reads it.
    assert int(app.redis.get("fitzflix:plex:history-cursor")) == cursor + 99


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
        # An old entry at the cursor is already processed.
        {"type": "movie", "viewedAt": cursor, "accountID": 1, "ratingKey": "10"},
        {"type": "movie", "viewedAt": cursor + 100, "accountID": 1, "ratingKey": "42"},
        # An episode advances the cursor, but the poller does not record
        # it.
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


def test_poller_ignores_live_tv_entries(app, plex_server):
    """Make sure the poller ignores live TV entries.

    A live TV history entry has the movie type (#182). It advances the
    cursor, but it never enqueues a watch, even with a guid that
    resolves."""

    cursor = int(time.time()) - 3600
    app.redis.set("fitzflix:plex:history-cursor", cursor)
    plex_server.history = [
        {
            "type": "movie",
            "viewedAt": cursor + 100,
            "accountID": 1,
            "ratingKey": "42",
            "live": "1",
        }
    ]
    plex_server.accounts = [{"id": 1, "name": "glenn-plex"}]
    plex_server.guids = {"42": "tmdb://578"}

    with app.app_context():
        assert plex_history_poll() is True

    assert plex_jobs(app) == []
    assert int(app.redis.get("fitzflix:plex:history-cursor")) == cursor + 100


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

        # A later poll that sees the same rating key skips the metadata
        # fetch.

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

    # A second user cannot claim the same Plex account.

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

    # A blank value removes the mapping.

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


def test_letterboxd_import_merges_with_plex_timed_row(app):
    """Make sure a Letterboxd import merges with a timed Plex row.

    A Letterboxd entry for a date that already has a Plex watch (with a
    clock time) updates that row. It does not add a second row."""

    from app.videos import apply_letterboxd_import, apply_plex_watch

    with app.app_context():
        user = User.query.first()
        user_plex = user.plex_username
        user.plex_username = "merge-drill"
        db.session.commit()
        try:
            movie = make_movie("Cross Source Film", 1969, tmdb_id=77321)
            db.session.commit()
            movie_id = movie.id

            assert (
                apply_plex_watch(
                    77321, "merge-drill", "2026-08-05T20:30:00+00:00", "webhook"
                )
                is True
            )
            films = [
                {
                    "title": "Cross Source Film",
                    "year": 1969,
                    "movie_id": movie_id,
                    "rating": 4.0,
                    "liked": True,
                    "entries": [
                        {
                            "watched": "2026-08-05",
                            "logged": "2026-08-06",
                            "rating": 4.0,
                            "review": "Merged in from Letterboxd.",
                            "rewatch": False,
                        }
                    ],
                }
            ]
            assert apply_letterboxd_import(user.id, films) is True

            db.session.expire_all()
            rows = UserMovieReview.query.filter_by(
                user_id=user.id, movie_id=movie_id
            ).all()
            assert len(rows) == 1
            # The Plex clock time survives. The Letterboxd data merges in.
            expected = (
                datetime.fromisoformat("2026-08-05T20:30:00+00:00")
                .astimezone()
                .replace(tzinfo=None)
            )
            assert rows[0].date_watched == expected
            assert rows[0].rating == 4.0
            assert rows[0].review == "Merged in from Letterboxd."
        finally:
            db.session.rollback()
            User.query.first().plex_username = user_plex
            db.session.commit()


def _letterboxd_film(movie_id, *watched):
    return {
        "title": "Late Show",
        "year": 1977,
        "movie_id": movie_id,
        "rating": 4.0,
        "liked": False,
        "entries": [
            {
                "watched": day,
                "logged": day,
                "rating": 4.0,
                "review": "",
                "rewatch": False,
            }
            for day in watched
        ],
    }


def test_a_reimport_finds_the_scrobble_that_the_feed_gave_to_the_day_before(app):
    """The feed gave a 00:10 scrobble of 10-07 to the entry of 10-06 (#381).

    A CSV import of 10-06 updates that row. It does not add a no-guid
    row that the export sends back to Letterboxd. A CSV entry of 10-07
    keeps its own row."""

    from app.videos import apply_letterboxd_import

    with app.app_context():
        user = User.query.first()
        movie = make_movie("Late Show", 1977, tmdb_id=77401)
        db.session.add(
            UserMovieReview(
                user_id=user.id,
                movie_id=movie.id,
                review="",
                date_watched=datetime(2026, 10, 7, 0, 10),
                letterboxd_guid="letterboxd-watch-9001",
                rating=4.0,
            )
        )
        db.session.commit()
        movie_id = movie.id

        assert apply_letterboxd_import(
            user.id, [_letterboxd_film(movie_id, "2026-10-06")]
        )
        rows = UserMovieReview.query.filter_by(user_id=user.id, movie_id=movie_id)
        assert rows.count() == 1

        assert apply_letterboxd_import(
            user.id, [_letterboxd_film(movie_id, "2026-10-06", "2026-10-07")]
        )
        days = sorted(row.date_watched for row in rows)
        assert days == [datetime(2026, 10, 6), datetime(2026, 10, 7, 0, 10)]


def test_a_scrobble_twin_goes_to_1_entry_of_an_import(app):
    """Entries of 10-06 and 10-08 and a 00:10 scrobble on 10-07 (#381 follow-up).

    The scrobble is the twin of 10-06 only. The 10-08 entry gets its own
    row, and it does not overwrite the verdict of 10-06."""

    from app.videos import apply_letterboxd_import

    with app.app_context():
        user = User.query.first()
        movie = make_movie("Late Show Twice", 1978, tmdb_id=77402)
        db.session.add(
            UserMovieReview(
                user_id=user.id,
                movie_id=movie.id,
                review="",
                date_watched=datetime(2026, 10, 7, 0, 10),
                letterboxd_guid="letterboxd-watch-9002",
            )
        )
        db.session.commit()
        movie_id = movie.id

        film = _letterboxd_film(movie_id, "2026-10-06", "2026-10-08")
        film["entries"][0]["rating"] = 3.0
        film["entries"][1]["rating"] = 5.0
        assert apply_letterboxd_import(user.id, [film])
        rows = sorted(
            UserMovieReview.query.filter_by(user_id=user.id, movie_id=movie_id),
            key=lambda row: row.date_watched,
        )
        assert [(row.date_watched, row.rating) for row in rows] == [
            (datetime(2026, 10, 7, 0, 10), 3.0),
            (datetime(2026, 10, 8), 5.0),
        ]


def test_review_edit_preserves_plex_timestamp(app, admin_client):
    """Make sure a review edit keeps the Plex timestamp.

    An edit of a viewing that keeps its date keeps the stored clock
    time. An edit that changes the date stores a new timestamp."""

    import re

    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.first().id
        movie = make_movie("Timestamped Film", 1988)
        row = UserMovieReview(
            user_id=user_id,
            movie_id=movie.id,
            review="",
            date_watched=datetime(2026, 8, 9, 17, 49, 14),
            rewatch=False,
            **star_rating_fields(None),
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    page = admin_client.get(f"/review/{row_id}/edit").get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    admin_client.post(
        f"/review/{row_id}/edit",
        data={
            "csrf_token": token,
            "rating": "4",
            "date_watched": "2026-08-09",
            "review": "Same date, time kept.",
            "review_submit": "Save Review",
        },
    )
    with app.app_context():
        db.session.expire_all()
        assert db.session.get(UserMovieReview, row_id).date_watched == datetime(
            2026, 8, 9, 17, 49, 14
        )

    page = admin_client.get(f"/review/{row_id}/edit").get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    admin_client.post(
        f"/review/{row_id}/edit",
        data={
            "csrf_token": token,
            "rating": "4",
            "date_watched": "2026-08-07",
            "review": "Same date, time kept.",
            "review_submit": "Save Review",
        },
    )
    with app.app_context():
        db.session.expire_all()
        assert db.session.get(UserMovieReview, row_id).date_watched == datetime(
            2026, 8, 7
        )


def test_a_deleted_history_item_does_not_hold_the_cursor(app, plex_server, monkeypatch):
    """Plex keeps the history of a deleted item. Its lookup gives 404 (#379)."""

    import requests

    from app import plex_api

    cursor = int(time.time()) - 3600
    app.redis.set("fitzflix:plex:history-cursor", cursor)
    plex_server.history = [
        {"type": "movie", "viewedAt": cursor + 100, "accountID": 1, "ratingKey": "60"},
        {"type": "movie", "viewedAt": cursor + 200, "accountID": 1, "ratingKey": "61"},
    ]
    plex_server.accounts = [{"id": 1, "name": "glenn-plex"}]
    plex_server.guids = {"61": "tmdb://12"}
    real_get = plex_server.get

    def deleted_item(url, *args, **kwargs):
        if url.endswith("/library/metadata/60"):
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("404 Not Found", response=response)
        return real_get(url, *args, **kwargs)

    monkeypatch.setattr(plex_api.requests, "get", deleted_item)

    # A restored library can bring the item back with the same key. Thus,
    # 2 polls hold the cursor before the 404 (#379 follow-up).

    with app.app_context():
        for _ in range(2):
            assert plex_history_poll() is True
            assert int(app.redis.get("fitzflix:plex:history-cursor")) == cursor + 99
        assert plex_history_poll() is True
    assert int(app.redis.get("fitzflix:plex:history-cursor")) == cursor + 200
    assert 0 < app.redis.ttl("fitzflix:plex:tmdb:60") <= 3600
    assert {job.args[0] for job in plex_jobs(app)} == {12}


def test_an_item_that_comes_back_after_a_404_is_recorded(app, plex_server, monkeypatch):
    """The Oct 16 shape: the item is missing for 1 poll, then restored (#379 follow-up)."""

    import requests

    from app import plex_api

    cursor = int(time.time()) - 3600
    app.redis.set("fitzflix:plex:history-cursor", cursor)
    plex_server.history = [
        {"type": "movie", "viewedAt": cursor + 100, "accountID": 1, "ratingKey": "62"}
    ]
    plex_server.accounts = [{"id": 1, "name": "glenn-plex"}]
    plex_server.guids = {"62": "tmdb://13"}
    real_get = plex_server.get
    missing = [True]

    def wiped(url, *args, **kwargs):
        if url.endswith("/library/metadata/62") and missing[0]:
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("404 Not Found", response=response)
        return real_get(url, *args, **kwargs)

    monkeypatch.setattr(plex_api.requests, "get", wiped)
    with app.app_context():
        assert plex_history_poll() is True
        assert plex_jobs(app) == []
        missing[0] = False
        assert plex_history_poll() is True
    assert [job.args[0] for job in plex_jobs(app)] == [13]
    assert int(app.redis.get("fitzflix:plex:history-cursor")) == cursor + 100
    assert not app.redis.exists("fitzflix:plex:missing:62")


def test_a_history_watch_read_again_adds_to_the_priority_once(app):
    """The day marker lasts 2 days. A watch that the poll reads again later counts once (#379)."""

    from app.diary import PLEX_WATCH_KEY

    viewed_at = "2026-08-02T19:00:00+00:00"
    with app.app_context():
        movie = make_movie("Read Twice", 2001, tmdb_id=4801)
        db.session.commit()
        assert apply_plex_watch(4801, "glenn-plex", viewed_at, "history") is True
        for key in app.redis.scan_iter(
            PLEX_WATCH_KEY.format(username="*", tmdb_id=4801, day="*")
        ):
            app.redis.delete(key)
        assert apply_plex_watch(4801, "account-1", viewed_at, "history") is True
        db.session.expire_all()
        assert db.session.get(Movie, movie.id).shopping_cart_priority == 1


def test_a_failed_accounts_call_uses_the_last_names(app, plex_server, monkeypatch):
    """The username of the watch stays the name the webhook gives (#379)."""

    from app import plex_api

    cursor = int(time.time()) - 3600
    app.redis.set("fitzflix:plex:history-cursor", cursor)
    plex_server.history = [
        {"type": "movie", "viewedAt": cursor + 100, "accountID": 1, "ratingKey": "42"}
    ]
    plex_server.accounts = [{"id": 1, "name": "glenn-plex"}]
    plex_server.guids = {"42": "tmdb://578"}
    with app.app_context():
        assert plex_history_poll() is True

    real_get = plex_server.get

    def accounts_down(url, *args, **kwargs):
        if "/accounts" in url:
            raise TimeoutError("Plex is busy")
        return real_get(url, *args, **kwargs)

    monkeypatch.setattr(plex_api.requests, "get", accounts_down)
    plex_server.history.append(
        {"type": "movie", "viewedAt": cursor + 200, "accountID": 1, "ratingKey": "42"}
    )
    with app.app_context():
        assert plex_history_poll() is True
    assert [job.args[1] for job in plex_jobs(app)] == ["glenn-plex", "glenn-plex"]


def test_a_scrobble_after_midnight_joins_the_letterboxd_row_of_the_day_before(
    app, mapped_admin
):
    """A film that ran past midnight has its Letterboxd row on the day before (#425).

    The feed row has a midnight stamp. A Plex watch before noon of the
    next day is the same viewing. A watch in the afternoon is a new
    viewing."""

    from app.videos import star_rating_fields

    with app.app_context():
        movie = make_movie("Past Midnight", 1982, tmdb_id=4425)
        db.session.add(
            UserMovieReview(
                user_id=mapped_admin,
                movie_id=movie.id,
                letterboxd_guid="letterboxd-review-4425",
                date_watched=datetime(2026, 7, 18),
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()
        movie_id = movie.id

        after_midnight = datetime(2026, 7, 19, 0, 10).astimezone().isoformat()
        assert apply_plex_watch(4425, "glenn-plex", after_midnight, "webhook") is True
        db.session.expire_all()
        assert UserMovieReview.query.filter_by(movie_id=movie_id).count() == 1

        afternoon = datetime(2026, 7, 20, 15, 0).astimezone().isoformat()
        assert apply_plex_watch(4425, "glenn-plex", afternoon, "webhook") is True
        db.session.expire_all()
        assert UserMovieReview.query.filter_by(movie_id=movie_id).count() == 2


def test_a_watch_in_the_cursor_second_is_not_lost(app, plex_server):
    """Plex can list a second watch of the cursor second only on the next poll (#425).

    The next poll reads that second again. It queues the new watch and
    skips the watch that it queued already."""

    cursor = int(time.time()) - 3600
    app.redis.set("fitzflix:plex:history-cursor", cursor)
    plex_server.accounts = [{"id": 1, "name": "glenn-plex"}]
    plex_server.guids = {"11": "tmdb://578", "12": "tmdb://11"}
    first = {
        "type": "movie",
        "viewedAt": cursor + 100,
        "accountID": 1,
        "ratingKey": "11",
        "historyKey": "/status/sessions/history/11",
    }
    plex_server.history = [first]
    with app.app_context():
        assert plex_history_poll() is True

    plex_server.history = [
        first,
        {**first, "ratingKey": "12", "historyKey": "/status/sessions/history/12"},
    ]
    with app.app_context():
        assert plex_history_poll() is True

    assert [job.args[0] for job in plex_jobs(app)] == [578, 11]
    assert int(app.redis.get("fitzflix:plex:history-cursor")) == cursor + 100

    # A third poll with no new entry queues nothing.
    with app.app_context():
        assert plex_history_poll() is True
    assert len(plex_jobs(app)) == 2


def test_a_failed_lookup_in_the_cursor_second_queues_no_repeats(
    app, plex_server, monkeypatch
):
    """A failed lookup in the cursor second holds the cursor there (follow-up #425).

    The cursor never goes back, and the seen set keeps the handled
    entries. Thus, the next polls read only the failed entry again."""

    from app import plex_api

    cursor = int(time.time()) - 3600
    app.redis.set("fitzflix:plex:history-cursor", cursor)
    handled = {
        "type": "movie",
        "viewedAt": cursor,
        "accountID": 1,
        "ratingKey": "21",
        "historyKey": "/status/sessions/history/21",
    }
    failing = {
        **handled,
        "ratingKey": "22",
        "historyKey": "/status/sessions/history/22",
    }
    plex_server.history = [handled, failing]
    plex_server.accounts = [{"id": 1, "name": "glenn-plex"}]
    plex_server.guids = {"21": "tmdb://578", "22": "tmdb://11"}
    real_get = plex_server.get
    state = {"down": True}

    def flaky_get(url, *args, **kwargs):
        if state["down"] and url.endswith("/library/metadata/22"):
            raise TimeoutError("Plex is busy")
        return real_get(url, *args, **kwargs)

    monkeypatch.setattr(plex_api.requests, "get", flaky_get)
    for _ in range(3):
        with app.app_context():
            assert plex_history_poll() is True
        assert int(app.redis.get("fitzflix:plex:history-cursor")) == cursor
    assert [job.args[0] for job in plex_jobs(app)] == [578]

    state["down"] = False
    for _ in range(2):
        with app.app_context():
            assert plex_history_poll() is True
    assert [job.args[0] for job in plex_jobs(app)] == [578, 11]


@pytest.mark.parametrize(
    "payload",
    [
        [1, 2],
        "media.scrobble",
        {"event": "media.scrobble", "Metadata": "movie"},
        {
            "event": "media.scrobble",
            "Metadata": {"type": "movie", "title": "Jaws", "Guid": "tmdb://578"},
        },
        {
            "event": "media.scrobble",
            "Metadata": {"type": "movie", "title": "Jaws", "Guid": ["tmdb://578", 5]},
        },
        {
            "event": "media.scrobble",
            "Metadata": {"type": "movie", "title": "Jaws", "guid": 578},
        },
    ],
)
def test_a_shaped_webhook_body_gets_a_204(app, client, payload):
    """A list body or a string Guid gets the documented 204, not a 500 (#449)."""

    assert post_webhook(client, payload).status_code == 204
    raw = client.post(
        WEBHOOK, data=json.dumps(payload), content_type="application/json"
    )
    assert raw.status_code == 204
    assert plex_jobs(app) == []
