"""The Criterion24/7 now-playing card (#63): parsing the whatsonnow
page (countdown typo included), the film info page, the self-scheduling
poller, and the landing-page card's gating and staleness."""

import json

from datetime import datetime, timedelta

WHATSON_HTML = """
<div class="whatson__container--desktop">
    <p class="whatson__eyebrow">What's on <span class="whatson__eyebrow--bold">now:</span></p>
    <h2 class="whatson__title">Shock Corridor</h2>
    <div class="whatson__channel-buttons">
        <a href="https://www.criterionchannel.com/events/criterion-24-7" class="whatson__channel-link whatson__channel-link--live">
            <span class="whatson__channel-link-text whatson__channel-link-text--live">Watch Live</span>
        </a>
        <a href="https://www.criterionchannel.com/shock-corridor" class="whatson__channel-link whatson__channel-link--more">
            <span class="whatson__channel-link-text">More</span>
        </a>
    </div>
    <p class="whatson__eyebrow">Next film starts in: <span class="whatson__eyebrow--bold">1 hour 23 minutes</snap></p>
</div>
"""

INFO_HTML = """
<p>Directed by Samuel Fuller • 1963 • United States
<br>Starring Peter Breck, Constance Towers, Gene Evans</p>
"""


def subscribe_criterion(app):
    """Subscribe the admin user to the Criterion Channel."""

    from app import db
    from app.leaving_criterion import CRITERION_PROVIDER_ID
    from app.models import User, UserStreamingProvider

    with app.app_context():
        user = User.query.filter_by(admin=True).first()
        db.session.add(
            UserStreamingProvider(
                user_id=user.id,
                provider_id=CRITERION_PROVIDER_ID,
                name="Criterion Channel",
                logo_path="/criterion.jpg",
            )
        )
        db.session.commit()
        return user.id


def test_parse_whatson_page_reads_title_more_and_countdown(app):
    from app.criterion_now import parse_whatson_page

    title, more_url, minutes = parse_whatson_page(WHATSON_HTML)
    assert title == "Shock Corridor"
    assert more_url == "https://www.criterionchannel.com/shock-corridor"
    assert minutes == 83

    # Minutes-only countdown, still behind the literal </snap> typo
    title, _, minutes = parse_whatson_page(
        WHATSON_HTML.replace("1 hour 23 minutes", "2 minutes")
    )
    assert minutes == 2

    # Unreadable countdown parses as None, never a guess
    _, _, minutes = parse_whatson_page(
        WHATSON_HTML.replace("1 hour 23 minutes", "moments")
    )
    assert minutes is None

    assert parse_whatson_page("<html>redesigned</html>") == (None, None, None)


def test_parse_film_info_reads_the_meta_lines(app):
    from app.criterion_now import parse_film_info

    info = parse_film_info(INFO_HTML)
    assert info == {
        "director": "Samuel Fuller",
        "year": 1963,
        "country": "United States",
        "starring": "Peter Breck, Constance Towers, Gene Evans",
    }
    assert parse_film_info("<html>bare</html>") == {
        "director": None,
        "year": None,
        "country": None,
        "starring": None,
    }


def test_poller_stores_film_and_reschedules(app, monkeypatch):
    import app.criterion_now as criterion_now

    def fake_requests_get(url, timeout=None):
        class FakeResponse:
            text = WHATSON_HTML if "whatsonnow" in url else INFO_HTML

            def raise_for_status(self):
                """Never an HTTP error."""

        return FakeResponse()

    monkeypatch.setattr(criterion_now.requests, "get", fake_requests_get)
    monkeypatch.setattr(criterion_now, "match_tmdb_id", lambda title, year: 33667)
    monkeypatch.setattr(
        criterion_now,
        "enriched_movie",
        lambda tmdb_id: {
            "poster_path": "/shock.jpg",
            "runtime": 101,
            "crew": [{"id": 8556, "name": "Samuel Fuller", "job": "Director"}],
        },
    )

    assert criterion_now.poll_criterion_now() is True

    stored = json.loads(app.redis.get(criterion_now.NOW_KEY))
    assert stored["title"] == "Shock Corridor"
    assert stored["year"] == 1963
    assert stored["director"] == "Samuel Fuller"
    assert stored["tmdb_id"] == 33667
    assert stored["poster_path"] == "/shock.jpg"
    ends_at = datetime.strptime(stored["ends_at"], "%Y-%m-%d %H:%M:%S")
    assert timedelta(minutes=80) < ends_at - datetime.now() < timedelta(minutes=85)

    # The next poll is scheduled just past the countdown, single-file
    # under the deterministic job id

    with app.app_context():
        registry = app.maintenance_queue.scheduled_job_registry
        assert criterion_now.POLL_JOB_ID in registry.get_job_ids()

        # A second run replaces the scheduled poll instead of stacking
        criterion_now.poll_criterion_now()
        assert registry.get_job_ids().count(criterion_now.POLL_JOB_ID) == 1


def test_director_mismatch_degrades_to_a_plain_card(app, monkeypatch):
    """A wrong search hit must never dress the wrong film's poster over
    the right title: when TMDb's credited director disagrees with the
    Channel's, the film stores unmatched."""

    import app.criterion_now as criterion_now

    monkeypatch.setattr(criterion_now, "match_tmdb_id", lambda title, year: 99999)
    monkeypatch.setattr(
        criterion_now,
        "enriched_movie",
        lambda tmdb_id: {
            "poster_path": "/wrong-film.jpg",
            "crew": [{"id": 1, "name": "Alan Smithee", "job": "Director"}],
        },
    )
    with app.app_context():
        assert criterion_now.matched_film(
            "Shock Corridor",
            {
                "director": "Samuel Fuller",
                "year": 1963,
                "country": "United States",
                "starring": None,
            },
        ) == (None, None)


def test_card_gates_on_subscription_and_staleness(app, admin_client):
    import app.criterion_now as criterion_now

    def plant(ends_delta):
        app.redis.set(
            criterion_now.NOW_KEY,
            json.dumps(
                {
                    "title": "Shock Corridor",
                    "year": 1963,
                    "director": "Samuel Fuller",
                    "country": "United States",
                    "starring": "Peter Breck",
                    "more_url": "https://www.criterionchannel.com/shock-corridor",
                    "tmdb_id": 33667,
                    "poster_path": "/shock.jpg",
                    "ends_at": (datetime.now() + ends_delta).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
            ),
        )

    # Not a Criterion subscriber: no card even with a stored film

    plant(timedelta(minutes=45))
    body = admin_client.get("/").get_data(as_text=True)
    assert "On Criterion24/7 now" not in body

    subscribe_criterion(app)
    body = admin_client.get("/").get_data(as_text=True)
    assert "On Criterion24/7 now" in body
    assert "Shock Corridor (1963)" in body
    assert "Directed by Samuel Fuller" in body
    assert "Starring Peter Breck" in body
    assert "/shock.jpg" in body
    assert "Next film around" in body
    assert "criterionchannel.com/events/criterion-24-7" in body

    # A film just past its end keeps showing (the poller is about to
    # replace it) but drops the countdown line

    plant(timedelta(minutes=-5))
    body = admin_client.get("/").get_data(as_text=True)
    assert "Shock Corridor (1963)" in body
    assert "Next film around" not in body

    # Long past the end the poller must be broken — hide, don't lie

    plant(timedelta(minutes=-30))
    body = admin_client.get("/").get_data(as_text=True)
    assert "On Criterion24/7 now" not in body
