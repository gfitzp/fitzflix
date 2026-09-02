"""Test the watchlist availability alerts (#156/#230).

These tests cover the nightly snapshot diff, the 3 event kinds and
their precedence, the leaving-Criterion warning, the digest batching
and duplicate removal, the Profile opt-ins, and the recently-available
badge on the watchlist page."""

import json

from datetime import date, timedelta

from tests.conftest import ADMIN_EMAIL, MEMBER_EMAIL
from tests.factories import make_movie, make_movie_file
from tests.test_streaming import csrf_token_from, plant_availability, subscribe

NETFLIX = {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.jpg"}
CRITERION = {
    "provider_id": 258,
    "provider_name": "Criterion Channel",
    "logo_path": "/c.jpg",
}

EMPTY = {"link": None, "flatrate": [], "ads": [], "rent": [], "buy": []}


def watchlist_movie(app, title, tmdb_id, email=MEMBER_EMAIL, owned=False, **kwargs):
    """Commit a movie on the watchlist of the given user and return its
    movie id."""

    from app import db
    from app.models import User, UserWatchlist

    with app.app_context():
        user = User.query.filter_by(email=email).one()
        movie = make_movie(title, 2020, tmdb_id=tmdb_id, **kwargs)
        if owned:
            make_movie_file(movie, "Bluray-1080p")
        db.session.add(UserWatchlist(user_id=user.id, movie_id=movie.id))
        db.session.commit()
        return movie.id


def set_alert_flags(app, email, availability=False, rentals=False):
    """Set the digest opt-in columns of a user directly."""

    from app import db
    from app.models import User

    with app.app_context():
        user = User.query.filter_by(email=email).one()
        user.notify_availability = availability
        user.notify_rentals = rentals
        db.session.commit()


def capture_mail(monkeypatch):
    """Redirect the digest sender into a list of {subject, ...} calls."""

    import app.availability_alerts as alerts

    sent = []
    monkeypatch.setattr(
        alerts,
        "task_send_email",
        lambda subject, **kwargs: sent.append({"subject": subject, **kwargs}),
    )
    return sent


def run_task(app, monkeypatch):
    """Run the nightly diff with external URLs that build outside a
    request.

    The digest templates link back to the site."""

    monkeypatch.setitem(app.config, "SERVER_NAME", "fitzflix.test")
    from app.availability_alerts import notify_watchlist_availability

    notify_watchlist_availability()


def reset_alert_users(app):
    """Set the alert columns of both persistent test users back to the
    default."""

    set_alert_flags(app, MEMBER_EMAIL)
    set_alert_flags(app, ADMIN_EMAIL)


def test_first_run_only_plants_snapshots(app, monkeypatch):
    """Test that a film that already streams at the first sighting never
    notifies.

    The first sighting creates the snapshot. The Plex history poller does
    the same."""

    sent = capture_mail(monkeypatch)
    set_alert_flags(app, MEMBER_EMAIL, availability=True)
    try:
        movie_id = watchlist_movie(app, "Planted", 9001)
        subscribe(app, 8, "Netflix", email=MEMBER_EMAIL)
        plant_availability(app, 9001, {**EMPTY, "flatrate": [NETFLIX]})

        run_task(app, monkeypatch)
        run_task(app, monkeypatch)

        assert sent == []
        with app.app_context():
            from app.availability_alerts import recent_availability
            from app.models import User

            user = User.query.filter_by(email=MEMBER_EMAIL).one()
            assert recent_availability(user) == {}
        assert movie_id
    finally:
        reset_alert_users(app)


def test_streaming_debut_mails_once_and_badges(app, monkeypatch):
    """Test a watchlisted film that appears on a subscribed service.

    Fitzflix mails 1 digest, stamps the badge record, and never repeats
    the digest."""

    sent = capture_mail(monkeypatch)
    set_alert_flags(app, MEMBER_EMAIL, availability=True)
    try:
        movie_id = watchlist_movie(app, "Debut", 9002, tmdb_poster_path="/debut.jpg")
        subscribe(app, 8, "Netflix", email=MEMBER_EMAIL)
        plant_availability(app, 9002, EMPTY)
        run_task(app, monkeypatch)
        assert sent == []

        plant_availability(app, 9002, {**EMPTY, "flatrate": [NETFLIX]})
        run_task(app, monkeypatch)

        assert len(sent) == 1
        assert sent[0]["subject"] == "Fitzflix - 1 watchlist film now available"
        assert sent[0]["recipients"] == [MEMBER_EMAIL]
        assert "Now on Netflix" in sent[0]["text_body"]
        assert "Debut (2020)" in sent[0]["text_body"]
        assert "Now on Netflix" in sent[0]["html_body"]

        # The HTML digest starts each film with its artwork. Here it is
        # the w154 rendition of TMDB, because the record has no custom
        # poster

        poster_url = app.config["TMDB_IMAGE_URL"] + "/w154/debut.jpg"
        assert poster_url in sent[0]["html_body"]
        assert poster_url not in sent[0]["text_body"]

        with app.app_context():
            from app.availability_alerts import recent_availability
            from app.models import User

            user = User.query.filter_by(email=MEMBER_EMAIL).one()
            recent = recent_availability(user)
            assert recent[movie_id]["label"] == "New on Netflix"

        run_task(app, monkeypatch)
        assert len(sent) == 1
    finally:
        reset_alert_users(app)


def test_debut_on_unsubscribed_service_is_silent(app, monkeypatch):
    """Test that a service without a subscription is not 'available'."""

    sent = capture_mail(monkeypatch)
    set_alert_flags(app, MEMBER_EMAIL, availability=True)
    try:
        watchlist_movie(app, "Elsewhere", 9003)
        subscribe(app, 258, "Criterion Channel", email=MEMBER_EMAIL)
        plant_availability(app, 9003, EMPTY)
        run_task(app, monkeypatch)
        plant_availability(app, 9003, {**EMPTY, "flatrate": [NETFLIX]})
        run_task(app, monkeypatch)
        assert sent == []
    finally:
        reset_alert_users(app)


def test_local_arrival_notifies_but_upgrades_stay_silent(app, monkeypatch):
    """Test that the first library copy of a film notifies.

    A second (upgrade) file for a film that the user already owns never
    notifies."""

    sent = capture_mail(monkeypatch)
    set_alert_flags(app, MEMBER_EMAIL, availability=True)
    try:
        from app import db
        from app.models import Movie

        movie_id = watchlist_movie(app, "Arrival", 9004)
        run_task(app, monkeypatch)
        assert sent == []

        with app.app_context():
            make_movie_file(db.session.get(Movie, movie_id), "DVD")
            db.session.commit()
        run_task(app, monkeypatch)
        assert len(sent) == 1
        assert "Added to the library" in sent[0]["text_body"]

        with app.app_context():
            make_movie_file(db.session.get(Movie, movie_id), "Bluray-2160p Remux")
            db.session.commit()
        run_task(app, monkeypatch)
        assert len(sent) == 1

        with app.app_context():
            from app.availability_alerts import recent_availability
            from app.models import User

            user = User.query.filter_by(email=MEMBER_EMAIL).one()
            assert recent_availability(user)[movie_id]["label"] == "New in library"
    finally:
        reset_alert_users(app)


def test_owned_films_skip_streaming_events(app, monkeypatch):
    """Test that the streaming debut of an owned watchlisted film is silent.

    The copy on the shelf already beats the stream in the bucket order."""

    sent = capture_mail(monkeypatch)
    set_alert_flags(app, MEMBER_EMAIL, availability=True)
    try:
        watchlist_movie(app, "Owned", 9005, owned=True)
        subscribe(app, 8, "Netflix", email=MEMBER_EMAIL)
        plant_availability(app, 9005, EMPTY)
        run_task(app, monkeypatch)
        plant_availability(app, 9005, {**EMPTY, "flatrate": [NETFLIX]})
        run_task(app, monkeypatch)
        assert sent == []
    finally:
        reset_alert_users(app)


def test_rentals_are_a_separate_opt_in(app, monkeypatch):
    """Test that a rental debut reaches only the users that asked for
    rentals."""

    sent = capture_mail(monkeypatch)
    set_alert_flags(app, MEMBER_EMAIL, availability=True, rentals=True)
    set_alert_flags(app, ADMIN_EMAIL, availability=True)
    try:
        movie_id = watchlist_movie(app, "Rentable", 9006, email=MEMBER_EMAIL)
        with app.app_context():
            from app import db
            from app.models import User, UserWatchlist

            admin = User.query.filter_by(email=ADMIN_EMAIL).one()
            db.session.add(UserWatchlist(user_id=admin.id, movie_id=movie_id))
            db.session.commit()
        subscribe(app, 10, "Amazon Video", email=MEMBER_EMAIL)
        subscribe(app, 10, "Amazon Video", email=ADMIN_EMAIL)
        amazon = {
            "provider_id": 10,
            "provider_name": "Amazon Video",
            "logo_path": "/a.jpg",
        }
        plant_availability(app, 9006, EMPTY)
        run_task(app, monkeypatch)
        plant_availability(app, 9006, {**EMPTY, "rent": [amazon]})
        run_task(app, monkeypatch)

        assert len(sent) == 1
        assert sent[0]["recipients"] == [MEMBER_EMAIL]
        assert "Available to rent on Amazon Video" in sent[0]["text_body"]
    finally:
        reset_alert_users(app)


def test_cache_gap_never_fakes_a_debut(app, monkeypatch):
    """Test a film with no cached availability tonight.

    The film keeps its old snapshot entry. Thus, the payload that comes
    back reads as no change."""

    from app.streaming import AVAILABILITY_KEY

    sent = capture_mail(monkeypatch)
    set_alert_flags(app, MEMBER_EMAIL, availability=True)
    try:
        watchlist_movie(app, "Flicker", 9007)
        subscribe(app, 8, "Netflix", email=MEMBER_EMAIL)
        plant_availability(app, 9007, {**EMPTY, "flatrate": [NETFLIX]})
        run_task(app, monkeypatch)

        app.redis.delete(AVAILABILITY_KEY.format(tmdb_id=9007))
        run_task(app, monkeypatch)
        plant_availability(app, 9007, {**EMPTY, "flatrate": [NETFLIX]})
        run_task(app, monkeypatch)
        assert sent == []
    finally:
        reset_alert_users(app)


def test_leaving_criterion_warning_fires_once_per_set(app, monkeypatch):
    """Test the warning for a watchlisted, unowned film in the stored
    leaving set.

    Fitzflix warns the Criterion subscribers in 1 digest, and only 1
    time. The final-week marker covers the second send when the set
    arrives late."""

    from app.leaving_criterion import LEAVING_KEY

    sent = capture_mail(monkeypatch)
    set_alert_flags(app, MEMBER_EMAIL, availability=True)
    try:
        watchlist_movie(app, "Departing", 9008)
        subscribe(app, 258, "Criterion Channel", email=MEMBER_EMAIL)
        departs = date.today() + timedelta(days=5)
        app.redis.set(
            LEAVING_KEY,
            json.dumps(
                {
                    "fetched_at": "2026-08-26 03:30",
                    "departs": departs.isoformat(),
                    "items": [{"tmdb_id": 9008, "title": "Departing"}],
                }
            ),
        )

        run_task(app, monkeypatch)
        assert len(sent) == 1
        assert (
            sent[0]["subject"]
            == "Fitzflix - Watchlist films leaving the Criterion Channel"
        )
        assert "Leaving the Criterion Channel" in sent[0]["text_body"]

        run_task(app, monkeypatch)
        assert len(sent) == 1
    finally:
        reset_alert_users(app)


def test_leaving_warning_needs_a_criterion_subscription(app, monkeypatch):
    """Test that non-subscribers get no warning.

    They cannot watch the film there in any case."""

    from app.leaving_criterion import LEAVING_KEY

    sent = capture_mail(monkeypatch)
    set_alert_flags(app, MEMBER_EMAIL, availability=True)
    try:
        watchlist_movie(app, "Not Mine", 9009)
        departs = date.today() + timedelta(days=5)
        app.redis.set(
            LEAVING_KEY,
            json.dumps({"departs": departs.isoformat(), "items": [{"tmdb_id": 9009}]}),
        )
        run_task(app, monkeypatch)
        assert sent == []
    finally:
        reset_alert_users(app)


def test_digest_stays_unsent_without_opt_in(app, monkeypatch):
    """Test that the badge record is stamped for everyone, but mail is
    strictly opt-in."""

    sent = capture_mail(monkeypatch)
    movie_id = watchlist_movie(app, "Quiet", 9010)
    subscribe(app, 8, "Netflix", email=MEMBER_EMAIL)
    plant_availability(app, 9010, EMPTY)
    run_task(app, monkeypatch)
    plant_availability(app, 9010, {**EMPTY, "flatrate": [NETFLIX]})
    run_task(app, monkeypatch)

    assert sent == []
    with app.app_context():
        from app.availability_alerts import recent_availability
        from app.models import User

        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        assert recent_availability(user)[movie_id]["label"] == "New on Netflix"


def test_movie_states_serves_recent_fold_and_prunes_stale(app, user_client):
    """Test the green-fold label in /movie_states.

    The label is present for the films that the diff found newly
    available in the last month. Older records answer None, and the
    store removes them on read."""

    from app.availability_alerts import RECENT_KEY

    movie_id = watchlist_movie(app, "Badged", 9011)
    stale_id = watchlist_movie(app, "Stale", 9012)
    with app.app_context():
        from app.models import User

        user_id = User.query.filter_by(email=MEMBER_EMAIL).one().id
    key = RECENT_KEY.format(user_id=user_id)
    app.redis.hset(
        key,
        str(movie_id),
        json.dumps({"date": date.today().isoformat(), "label": "New on Netflix"}),
    )
    app.redis.hset(
        key,
        str(stale_id),
        json.dumps(
            {
                "date": (date.today() - timedelta(days=40)).isoformat(),
                "label": "New in library",
            }
        ),
    )

    payload = user_client.get(
        f"/movie_states?movie_ids={movie_id},{stale_id}"
    ).get_json()
    assert payload["movies"][str(movie_id)]["fold_new"] == "New on Netflix"
    assert payload["movies"][str(stale_id)]["fold_new"] is None
    assert app.redis.hget(key, str(stale_id)) is None


def test_movie_states_serves_leaving_fold_to_subscribers_only(
    app, user_client, admin_client
):
    """Test the red-fold departure date in /movie_states.

    The date is present for the films in the stored leaving set. Criterion
    subscribers get it by movie id and by bare tmdb id. Non-subscribers
    get None."""

    from app.leaving_criterion import LEAVING_KEY

    movie_id = watchlist_movie(app, "Going Soon", 9013)
    subscribe(app, 258, "Criterion Channel", email=MEMBER_EMAIL)
    departs = date.today() + timedelta(days=5)
    app.redis.set(
        LEAVING_KEY,
        json.dumps(
            {
                "departs": departs.isoformat(),
                "items": [{"tmdb_id": 9013}, {"tmdb_id": 9014}],
            }
        ),
    )
    label = departs.strftime("%B %-d")

    payload = user_client.get(
        f"/movie_states?movie_ids={movie_id}&tmdb_ids=9014"
    ).get_json()
    assert payload["movies"][str(movie_id)]["fold_leaving"] == label
    assert payload["tmdb"]["9014"]["fold_leaving"] == label

    unsubscribed = admin_client.get(f"/movie_states?movie_ids={movie_id}").get_json()
    assert unsubscribed["movies"][str(movie_id)]["fold_leaving"] is None


def test_profile_saves_alert_opt_ins(app, user_client):
    """Test that the alert checkboxes on the Profile page write both
    columns.

    The section renders with the rentals framing."""

    from app.models import User

    try:
        page = user_client.get("/profile").get_data(as_text=True)
        assert "Watchlist Alerts" in page

        response = user_client.post(
            "/profile",
            data={
                "csrf_token": csrf_token_from(page),
                "notify_availability": "y",
                "notify_rentals": "y",
                "alerts_submit": "Save Alert Settings",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            user = User.query.filter_by(email=MEMBER_EMAIL).one()
            assert user.notify_availability is True
            assert user.notify_rentals is True

        page = user_client.get("/profile").get_data(as_text=True)
        response = user_client.post(
            "/profile",
            data={
                "csrf_token": csrf_token_from(page),
                "alerts_submit": "Save Alert Settings",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            user = User.query.filter_by(email=MEMBER_EMAIL).one()
            assert user.notify_availability is False
            assert user.notify_rentals is False
    finally:
        reset_alert_users(app)
