"""Watchlist availability alerts (#156/#230): the nightly snapshot
diff, the three event kinds and their precedence, the leaving-Criterion
warning, digest batching and dedup, the Profile opt-ins, and the
watchlist page's recently-available badge."""

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
    """A committed movie on the given user's watchlist; its movie id."""

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
    """Set a user's digest opt-in columns directly."""

    from app import db
    from app.models import User

    with app.app_context():
        user = User.query.filter_by(email=email).one()
        user.notify_availability = availability
        user.notify_rentals = rentals
        db.session.commit()


def capture_mail(monkeypatch):
    """Divert the digest sender into a list of {subject, ...} calls."""

    import app.availability_alerts as alerts

    sent = []
    monkeypatch.setattr(
        alerts,
        "task_send_email",
        lambda subject, **kwargs: sent.append({"subject": subject, **kwargs}),
    )
    return sent


def run_task(app, monkeypatch):
    """Run the nightly diff with external URLs buildable outside a
    request (the digest templates link back to the site)."""

    monkeypatch.setitem(app.config, "SERVER_NAME", "fitzflix.test")
    from app.availability_alerts import notify_watchlist_availability

    notify_watchlist_availability()


def reset_alert_users(app):
    """Put both persistent test users' alert columns back to default."""

    set_alert_flags(app, MEMBER_EMAIL)
    set_alert_flags(app, ADMIN_EMAIL)


def test_first_run_only_plants_snapshots(app, monkeypatch):
    """A film already streaming when first seen never notifies — the
    first sighting plants the snapshot, like the Plex history poller."""

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
    """A watchlisted film turning up on a subscribed service mails one
    digest, stamps the badge record, and never repeats."""

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

        # The HTML digest leads each film with its artwork — TMDB's
        # w154 rendition here, since the record has no custom poster

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
    """A service the user doesn't subscribe to isn't 'available'."""

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
    """The film's first library copy fires; a further (upgrade) file
    for an already-owned film never does."""

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
    """An owned watchlisted film's streaming debut says nothing — the
    copy on the shelf already beats the stream, per the bucket order."""

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
    """A rental debut reaches only users who asked for rentals."""

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
    """A film whose availability is uncached tonight keeps its old
    snapshot entry, so the payload coming back reads as no change."""

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
    """A watchlisted, unowned film in the stored leaving set warns
    Criterion subscribers in one digest, and only once — the final-week
    marker covers the second send when the set lands late."""

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
    """Non-subscribers can't watch it there anyway — no warning."""

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
    """The badge record is stamped for everyone, but mail is strictly
    opt-in."""

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


def test_watchlist_page_badges_recent_films_and_prunes_stale(app, user_client):
    """The watchlist tile badges films the diff found newly available
    in the last month; older records age out of the store on read."""

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

    page = user_client.get("/watchlist").get_data(as_text=True)
    assert "New on Netflix" in page
    assert "New in library" not in page
    assert app.redis.hget(key, str(stale_id)) is None


def test_watchlist_page_overlays_leaving_badge(app, user_client):
    """A watchlisted film on the user's Criterion subscription that's
    in the stored leaving set wears the red departure date overlaid on
    its poster tile."""

    from app.leaving_criterion import LEAVING_KEY

    watchlist_movie(app, "Going Soon", 9013)
    subscribe(app, 258, "Criterion Channel", email=MEMBER_EMAIL)
    plant_availability(app, 9013, {**EMPTY, "flatrate": [CRITERION]})
    departs = date.today() + timedelta(days=5)
    app.redis.set(
        LEAVING_KEY,
        json.dumps({"departs": departs.isoformat(), "items": [{"tmdb_id": 9013}]}),
    )

    page = user_client.get("/watchlist").get_data(as_text=True)
    assert f"Leaving {departs.strftime('%B %-d')}" in page


def test_profile_saves_alert_opt_ins(app, user_client):
    """The Profile page's alert checkboxes write both columns, and the
    section renders with the rentals framing."""

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
