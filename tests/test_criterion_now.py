"""The Criterion24/7 now-playing card (#63): parsing the whatsonnow
page (countdown typo included), the film info page, the self-scheduling
poller, and the landing-page card's gating, staleness, star row, and
filmography-linked credits."""

import json
import re

from datetime import datetime, timedelta


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


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


def test_parse_film_info_flattens_nonbreaking_spaces(app):
    """The Channel writes non-breaking spaces raw, as &nbsp;, and
    sometimes double-escaped (&amp;nbsp;) — the shelf once showed a
    literal "&nbsp;Hong Kong". All three must read as plain spaces."""

    from app.criterion_now import parse_film_info

    for nbsp in ("\xa0", "&nbsp;", "&amp;nbsp;"):
        info = parse_film_info(
            "<p>Directed by Wong Kar-wai • 2000 •"
            f"{nbsp}Hong{nbsp}Kong\n"
            f"<br>Starring Tony{nbsp}Leung, Maggie Cheung</p>"
        )
        assert info["country"] == "Hong Kong", repr(nbsp)
        assert info["starring"] == "Tony Leung, Maggie Cheung", repr(nbsp)


def test_parse_whatson_page_flattens_nonbreaking_spaces(app):
    from app.criterion_now import parse_whatson_page

    title, _, _ = parse_whatson_page(
        WHATSON_HTML.replace("Shock Corridor", "Shock&amp;nbsp;Corridor")
    )
    assert title == "Shock Corridor"


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


def test_heartbeat_skips_the_scrape_while_the_chain_is_alive(app, monkeypatch):
    """The half-hourly cron never rescans the showing film: while a
    poll is booked under the deterministic job id, the heartbeat does
    nothing (Glenn's report, Aug 2026 — the cron used to point at the
    poller itself and scraped unconditionally)."""

    from datetime import timedelta

    import app.criterion_now as criterion_now

    scrapes = []
    monkeypatch.setattr(
        criterion_now, "poll_criterion_now", lambda: scrapes.append(1) or True
    )

    with app.app_context():
        job = app.maintenance_queue.enqueue_in(
            timedelta(minutes=45),
            "app.criterion_now.poll_criterion_now",
            job_id=criterion_now.POLL_JOB_ID,
        )

        assert criterion_now.heartbeat_criterion_now() is True
        assert scrapes == []

        # The booked poll is untouched

        registry = app.maintenance_queue.scheduled_job_registry
        assert criterion_now.POLL_JOB_ID in registry.get_job_ids()

        # The normal healthy state LIES in the job hash: the poll
        # re-enqueues itself under its own executing id, and RQ writes
        # "finished" over the hash when that run completes — the
        # registry entry is the truth, and the heartbeat must trust
        # it, not the status (the live drill caught this, Aug 2026)

        job.set_status("finished")
        assert criterion_now.heartbeat_criterion_now() is True
        assert scrapes == []


def test_heartbeat_revives_a_dead_chain(app, monkeypatch):
    """No booked poll — the chain died, or the app just started — and
    the heartbeat runs the poller; likewise when the chain's last run
    finished without rescheduling."""

    import app.criterion_now as criterion_now

    scrapes = []
    monkeypatch.setattr(
        criterion_now, "poll_criterion_now", lambda: scrapes.append(1) or True
    )

    with app.app_context():
        assert criterion_now.heartbeat_criterion_now() is True
        assert scrapes == [1]

        # A finished job hash WITHOUT a registry entry (a run that
        # crashed before its re-enqueue) counts as dead too

        from rq.job import Job

        job = Job.create(
            print,
            connection=app.redis,
            id=criterion_now.POLL_JOB_ID,
        )
        job.set_status("finished")
        job.save()
        assert criterion_now.heartbeat_criterion_now() is True
        assert scrapes == [1, 1]


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


def test_director_match_survives_romanization_differences(app, monkeypatch):
    """TMDb credited 'Mabel Cheung Yuen-Ting' for An Autumn's Tale but
    the Channel says 'Mabel Cheung' — a fuller romanization on either
    side, reversed name order, or hyphen differences must still match."""

    import app.criterion_now as criterion_now

    monkeypatch.setattr(criterion_now, "match_tmdb_id", lambda title, year: 64015)
    monkeypatch.setattr(
        criterion_now,
        "enriched_movie",
        lambda tmdb_id: {
            "poster_path": "/autumn.jpg",
            "crew": [{"id": 1, "name": "Mabel Cheung Yuen-Ting", "job": "Director"}],
        },
    )
    with app.app_context():
        assert criterion_now.matched_film(
            "An Autumn's Tale",
            {
                "director": "Mabel Cheung",
                "year": 1987,
                "country": "Hong Kong",
                "starring": None,
            },
        ) == (64015, "/autumn.jpg")

    assert criterion_now._person_matches("Wong Kar-wai", "Kar-Wai Wong")
    assert not criterion_now._person_matches("Samuel Fuller", "Alan Smithee")


def test_starring_line_verifies_when_no_director_is_known(app, monkeypatch):
    """Without a director on both sides the Starring line stands in:
    one scraped name among TMDb's top billing keeps the match, a total
    miss degrades to a plain card. A cast miss alone must never veto a
    film whose director agrees — the enriched cast is only top billing."""

    import app.criterion_now as criterion_now

    payload = {
        "poster_path": "/autumn.jpg",
        "crew": [],
        "cast": [
            {"id": 1, "name": "Chow Yun-Fat"},
            {"id": 2, "name": "Cherie Chung Cho-Hung"},
        ],
    }
    monkeypatch.setattr(criterion_now, "match_tmdb_id", lambda title, year: 64015)
    monkeypatch.setattr(criterion_now, "enriched_movie", lambda tmdb_id: payload)

    info = {
        "director": None,
        "year": 1987,
        "country": "Hong Kong",
        "starring": "Cherie Chung, Chow Yun-fat and Danny Chan",
    }
    with app.app_context():
        assert criterion_now.matched_film("An Autumn's Tale", info) == (
            64015,
            "/autumn.jpg",
        )

        # The wrong film's billing shares nobody with the Channel's line
        payload["cast"] = [{"id": 3, "name": "Alan Smithee"}]
        assert criterion_now.matched_film("An Autumn's Tale", info) == (None, None)

        # But a matching director outranks a cast miss
        payload["crew"] = [{"id": 4, "name": "Mabel Cheung", "job": "Director"}]
        info["director"] = "Mabel Cheung"
        assert criterion_now.matched_film("An Autumn's Tale", info) == (
            64015,
            "/autumn.jpg",
        )


def plant_enriched(app, tmdb_id=33667):
    """Cache an enriched payload for the airing film, as the poller's
    match would have."""

    app.redis.set(
        f"fitzflix:tmdb:movie:{tmdb_id}:enriched",
        json.dumps(
            {
                "tmdb_id": tmdb_id,
                "title": "Shock Corridor",
                "year": "1963",
                "poster_path": "/shock.jpg",
                "runtime": 101,
                "overview": "A reporter has himself committed to crack a murder.",
                "original_language": "en",
                "genres": [{"id": 18, "name": "Drama"}],
                "keywords": [],
                "cast": [
                    {"id": 101, "name": "Peter Breck"},
                    {"id": 102, "name": "Constance Towers"},
                    {"id": 103, "name": "Gene Evans"},
                    {"id": 104, "name": "James Best"},
                ],
                "crew": [{"id": 8556, "name": "Samuel Fuller", "job": "Director"}],
            }
        ),
    )


def plant_profile(app, user_id):
    """A Drama-leaning profile with a calibration curve, so the card
    can carry an estimated rating."""

    app.redis.set(
        f"fitzflix:recs:profile:{user_id}",
        json.dumps(
            {
                "affinities": {
                    "genre:18": {
                        "class": "genre",
                        "label": "Drama",
                        "count": 3,
                        "score": 0.5,
                    }
                },
                "movies": 3,
                "calibration": {
                    "scores": [-0.5, 0.0, 0.5, 1.0],
                    "stars": [1.0, 2.5, 3.5, 4.5],
                },
            }
        ),
    )


def test_card_carries_the_estimate_and_linked_credits(app, admin_client, monkeypatch):
    import app.criterion_now as criterion_now
    import app.main.discover as discover

    # The TMDb log route fetches film details up front; feed it the
    # airing film so the first tap can create the record

    class FakeDetails:
        status_code = 200

        def raise_for_status(self):
            """Never an HTTP error."""

        def json(self):
            """The airing film's details."""

            return {
                "id": 33667,
                "title": "Shock Corridor",
                "release_date": "1963-09-11",
                "poster_path": "/shock.jpg",
                "genres": [{"id": 18, "name": "Drama"}],
                "credits": {"cast": []},
                "release_dates": {"results": []},
            }

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(discover, "tmdb_get", lambda *a, **k: FakeDetails())

    user_id = subscribe_criterion(app)
    plant_profile(app, user_id)
    plant_enriched(app)
    app.redis.set(
        criterion_now.NOW_KEY,
        json.dumps(
            {
                "title": "Shock Corridor",
                "year": 1963,
                "director": "Samuel Fuller",
                "starring": "Peter Breck, Constance Towers, Gene Evans",
                "more_url": "https://www.criterionchannel.com/shock-corridor",
                "tmdb_id": 33667,
                "poster_path": "/shock.jpg",
                "ends_at": (datetime.now() + timedelta(minutes=45)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        ),
    )

    body = admin_client.get("/").get_data(as_text=True)

    # Director and top-3 billed cast link to their filmography pages
    assert "credit=8556" in body
    assert ">Samuel Fuller</a>" in body
    assert "credit=101" in body
    assert "credit=103" in body
    assert "credit=104" not in body

    # The TMDb synopsis rides on the card

    assert "A reporter has himself committed to crack a murder." in body

    # The unlogged film previews the engine's estimate, posting to the
    # TMDb log route (no record exists yet)

    assert 'title="Estimated' in body
    assert 'action="/review/tmdb/33667"' in body

    # The first tap creates the record and answers ladder JSON in place

    response = admin_client.post(
        "/review/tmdb/33667",
        data={"quick_rating": "4", "csrf_token": csrf_token_from(body)},
        headers={"X-Requested-With": "ladder"},
    )
    assert response.status_code == 200
    assert response.get_json()["rating"] == 4.0

    from app.models import Movie, UserMovieReview

    with app.app_context():
        movie = Movie.query.filter_by(tmdb_id=33667).one()
        row = UserMovieReview.query.filter_by(movie_id=movie.id).one()
        assert float(row.rating) == 4.0
        movie_id = movie.id

    # With a record, the card's form aims at the movie route, showing
    # the real verdict instead of the estimate

    body = admin_client.get("/").get_data(as_text=True)
    assert f'action="/movie/{movie_id}"' in body
    assert 'title="Estimated' not in body

    # A ladder post still aimed at the TMDb route forwards with method
    # and body intact (307), landing in the movie route's toggle-off

    response = admin_client.post(
        "/review/tmdb/33667",
        data={"quick_rating": "4", "csrf_token": csrf_token_from(body)},
        headers={"X-Requested-With": "ladder"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert response.get_json()["rating"] is None

    with app.app_context():
        assert UserMovieReview.query.filter_by(movie_id=movie_id).count() == 0


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


def test_card_watchlist_toggle_and_minutes_in(app, admin_client):
    """#78 + #79: the card carries a watchlist toggle whose face
    follows the record, and 'About N minutes in' derived from the
    predicted end minus the runtime — never shown without both."""

    import re

    import app.criterion_now as criterion_now

    from app import db
    from app.models import UserWatchlist
    from tests.factories import make_movie

    user_id = subscribe_criterion(app)
    plant_enriched(app)

    def seed_now(minutes_left):
        app.redis.set(
            criterion_now.NOW_KEY,
            json.dumps(
                {
                    "title": "Shock Corridor",
                    "year": 1963,
                    "tmdb_id": 33667,
                    "poster_path": "/shock.jpg",
                    "ends_at": (
                        datetime.now() + timedelta(minutes=minutes_left)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                }
            ),
        )

    # 45 of the 101 minutes remain: about 56 minutes in; no record
    # yet, so the toggle shows Add and posts to the TMDb log route

    seed_now(45)
    body = admin_client.get("/").get_data(as_text=True)
    assert re.search(r"About 5[56] minutes in", body)
    assert "data-watchlist-scope" in body
    add_face = re.search(r'<button[^>]*name="add_watchlist_submit"[^>]*>', body)
    assert add_face and "d-none" not in add_face.group(0)
    assert body.count('action="/review/tmdb/33667"') >= 2  # ladder + toggle

    # A watchlisted local record flips the face and retargets the
    # movie route; a film that just started says so instead of "0
    # minutes in"

    with app.app_context():
        movie = make_movie("Shock Corridor", 1963, tmdb_id=33667)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        movie_id = movie.id

    seed_now(100)
    body = admin_client.get("/").get_data(as_text=True)
    assert "Just started" in body
    remove_face = re.search(r'<button[^>]*name="remove_watchlist_submit"[^>]*>', body)
    assert remove_face and "d-none" not in remove_face.group(0)
    add_face = re.search(r'<button[^>]*name="add_watchlist_submit"[^>]*>', body)
    assert add_face and "d-none" in add_face.group(0)
    assert f'action="/movie/{movie_id}"' in body

    # Past the predicted end the card lingers through STALE_GRACE,
    # but the film is over — claiming "About 106 minutes in" on a
    # 101-minute film would be a guess, so the line disappears

    seed_now(-5)
    body = admin_client.get("/").get_data(as_text=True)
    assert "On Criterion24/7 now" in body
    assert "minutes in" not in body
    assert "Just started" not in body

    # No runtime, no claim: an enrichment without one drops the line

    seed_now(45)
    app.redis.delete("fitzflix:tmdb:movie:33667:enriched")
    body = admin_client.get("/").get_data(as_text=True)
    assert "minutes in" not in body
    assert "Just started" not in body
