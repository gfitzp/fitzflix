"""Test the Criterion24/7 now-playing card.

These tests cover the parse of the whatsonnow page (with its countdown
typo), the film info page, the self-scheduling poller, and the card on
the landing page. For the card they cover the gating, the staleness,
the star row, and the credits with filmography links."""

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

    # A minutes-only countdown. It is still behind the literal </snap>
    # typo
    title, _, minutes = parse_whatson_page(
        WHATSON_HTML.replace("1 hour 23 minutes", "2 minutes")
    )
    assert minutes == 2

    # An unreadable countdown parses as None. Fitzflix never guesses
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
    """Test that all 3 forms of a non-breaking space read as plain spaces.

    The Channel writes non-breaking spaces raw, as &nbsp;, and sometimes
    double-escaped (&amp;nbsp;). The shelf showed a literal "&nbsp;Hong
    Kong" one time."""

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
    monkeypatch.setattr(
        criterion_now, "match_tmdb_id", lambda title, year, director=None: 33667
    )
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

    # Fitzflix schedules the next poll a short time after the countdown.
    # There is only 1 poll, under the deterministic job id

    with app.app_context():
        registry = app.maintenance_queue.scheduled_job_registry
        assert criterion_now.POLL_JOB_ID in registry.get_job_ids()

        # A second run replaces the scheduled poll. It does not add a
        # second poll
        criterion_now.poll_criterion_now()
        assert registry.get_job_ids().count(criterion_now.POLL_JOB_ID) == 1


def test_heartbeat_skips_the_scrape_while_the_chain_is_alive(app, monkeypatch):
    """Test that the half-hourly cron never rescans the film that shows.

    While a poll is booked under the deterministic job id, the heartbeat
    does nothing (reported by Glenn, 2026-08). Before, the cron pointed
    at the poller itself, and it scraped in every case."""

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

        # The booked poll is unchanged

        registry = app.maintenance_queue.scheduled_job_registry
        assert criterion_now.POLL_JOB_ID in registry.get_job_ids()

        # The job hash LIES in the normal healthy state. The poll
        # re-enqueues itself under its own executing id. Then RQ writes
        # "finished" over the hash when that run completes. The registry
        # entry is the truth. The heartbeat must trust the registry
        # entry, not the status (the live drill found this, 2026-08)

        job.set_status("finished")
        assert criterion_now.heartbeat_criterion_now() is True
        assert scrapes == []


def test_heartbeat_revives_a_dead_chain(app, monkeypatch):
    """Test that the heartbeat runs the poller when no poll is booked.

    This occurs when the chain died, or when the app just started. It
    also occurs when the last run of the chain finished without a
    reschedule."""

    import app.criterion_now as criterion_now

    scrapes = []
    monkeypatch.setattr(
        criterion_now, "poll_criterion_now", lambda: scrapes.append(1) or True
    )

    with app.app_context():
        assert criterion_now.heartbeat_criterion_now() is True
        assert scrapes == [1]

        # A finished job hash WITHOUT a registry entry (a run that
        # crashed before its re-enqueue) also counts as dead

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
    """Test that a wrong search hit never puts a wrong poster on the title.

    When the credited director from TMDB disagrees with the director from
    the Channel, Fitzflix stores the film as unmatched."""

    import app.criterion_now as criterion_now

    monkeypatch.setattr(
        criterion_now, "match_tmdb_id", lambda title, year, director=None: 99999
    )
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
    """Test that director name variants still match.

    TMDB credited 'Mabel Cheung Yuen-Ting' for An Autumn's Tale, but the
    Channel says 'Mabel Cheung'. A longer romanization on either side, a
    reversed name order, or a hyphen difference must still match."""

    import app.criterion_now as criterion_now

    monkeypatch.setattr(
        criterion_now, "match_tmdb_id", lambda title, year, director=None: 64015
    )
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
    """Test the Starring line as the fallback when a director is missing.

    Without a director on both sides, the Starring line replaces it. One
    scraped name in the top billing of TMDB keeps the match. A total miss
    degrades the film to a plain card. A cast miss alone must never veto
    a film with a director that agrees. The enriched cast is only the top
    billing."""

    import app.criterion_now as criterion_now

    payload = {
        "poster_path": "/autumn.jpg",
        "crew": [],
        "cast": [
            {"id": 1, "name": "Chow Yun-Fat"},
            {"id": 2, "name": "Cherie Chung Cho-Hung"},
        ],
    }
    monkeypatch.setattr(
        criterion_now, "match_tmdb_id", lambda title, year, director=None: 64015
    )
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

        # The billing of the wrong film has no name from the line of the
        # Channel
        payload["cast"] = [{"id": 3, "name": "Alan Smithee"}]
        assert criterion_now.matched_film("An Autumn's Tale", info) == (None, None)

        # But a director that matches outranks a cast miss
        payload["crew"] = [{"id": 4, "name": "Mabel Cheung", "job": "Director"}]
        info["director"] = "Mabel Cheung"
        assert criterion_now.matched_film("An Autumn's Tale", info) == (
            64015,
            "/autumn.jpg",
        )


def plant_enriched(app, tmdb_id=33667):
    """Cache an enriched payload for the film on air.

    The match of the poller does the same."""

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
    """Store a profile that prefers Drama, with a calibration curve.

    Thus, the card can show an estimated rating."""

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

    # The TMDB log route fetches the film details first. Give it the film
    # on air. Thus, the first tap can create the record

    class FakeDetails:
        status_code = 200

        def raise_for_status(self):
            """Never an HTTP error."""

        def json(self):
            """Return the details of the film on air."""

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

    # The director and the top 3 billed cast link to their filmography
    # pages
    assert "credit=8556" in body
    assert ">Samuel Fuller</a>" in body
    assert "credit=101" in body
    assert "credit=103" in body
    assert "credit=104" not in body

    # The TMDB synopsis goes with the card

    assert "A reporter has himself committed to crack a murder." in body

    # The unlogged film shows the estimate of the engine. It posts to the
    # TMDB log route (no record exists yet)

    assert 'title="Estimated' in body
    assert 'action="/review/tmdb/33667"' in body

    # The first tap creates the record and answers the ladder JSON in
    # place

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

    # With a record, the form of the card points at the movie route. It
    # shows the real verdict, not the estimate

    body = admin_client.get("/").get_data(as_text=True)
    assert f'action="/movie/{movie_id}"' in body
    assert 'title="Estimated' not in body

    # A ladder post that still points at the TMDB route forwards with the
    # method and the body unchanged (307). It goes into the toggle-off of
    # the movie route

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

    # Not a Criterion subscriber: no card, also with a stored film

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

    # A film a short time after its end still shows (the poller will
    # replace it soon). But the card removes the countdown line

    plant(timedelta(minutes=-5))
    body = admin_client.get("/").get_data(as_text=True)
    assert "Shock Corridor (1963)" in body
    assert "Next film around" not in body

    # A long time after the end, the poller must be broken. Hide the
    # card. Do not show wrong data

    plant(timedelta(minutes=-30))
    body = admin_client.get("/").get_data(as_text=True)
    assert "On Criterion24/7 now" not in body


def test_card_watchlist_toggle_and_minutes_in(app, admin_client):
    """Test the watchlist toggle and the 'About N minutes in' line.

    The face of the toggle follows the record. The line comes from the
    predicted end minus the runtime. The card never shows the line
    without both values."""

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

    # 45 of the 101 minutes remain: about 56 minutes in. There is no
    # record yet. Thus, the toggle shows Add and posts to the TMDB log
    # route

    seed_now(45)
    body = admin_client.get("/").get_data(as_text=True)
    assert re.search(r"About 5[56] minutes in", body)
    assert "data-watchlist-scope" in body
    add_face = re.search(r'<button[^>]*name="add_watchlist_submit"[^>]*>', body)
    assert add_face and "d-none" not in add_face.group(0)
    assert body.count('action="/review/tmdb/33667"') >= 2  # ladder + toggle

    # A watchlisted local record changes the face and points at the
    # movie route. A film that just started says so. It does not say "0
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

    # After the predicted end, the card stays through STALE_GRACE. But
    # the film is over. "About 106 minutes in" on a 101-minute film would
    # be a guess. Thus, the line disappears

    seed_now(-5)
    body = admin_client.get("/").get_data(as_text=True)
    assert "On Criterion24/7 now" in body
    assert "minutes in" not in body
    assert "Just started" not in body

    # No runtime, no claim: an enrichment without a runtime removes the
    # line

    seed_now(45)
    app.redis.delete("fitzflix:tmdb:movie:33667:enriched")
    body = admin_client.get("/").get_data(as_text=True)
    assert "minutes in" not in body
    assert "Just started" not in body


def test_card_fragment_follows_the_feed(app, admin_client):
    """Test the card refresh from /criterion-now.

    The home page fetches the card again from /criterion-now. Thus, an
    open tab follows the feed. The fragment carries the fingerprint of
    the film and a status line. The page uses them for its
    swap-or-repaint choice. The fragment comes back empty (not 404, not a
    page) when the card would hide. Thus, the container empties and does
    not freeze."""

    import app.criterion_now as criterion_now

    def plant(title, tmdb_id, minutes_left):
        app.redis.set(
            criterion_now.NOW_KEY,
            json.dumps(
                {
                    "title": title,
                    "year": 1963,
                    "tmdb_id": tmdb_id,
                    "poster_path": "/shock.jpg",
                    "ends_at": (
                        datetime.now() + timedelta(minutes=minutes_left)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                }
            ),
        )

    # Not a subscriber: no container on the page, and an empty fragment

    plant("Shock Corridor", 33667, 45)
    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="criterion-now"' not in body
    response = admin_client.get("/criterion-now")
    assert response.status_code == 200
    assert response.get_data(as_text=True).strip() == ""

    # The page of a subscriber wraps the card in the polling container.
    # The fragment is the same card: fingerprint, status line, ladder

    subscribe_criterion(app)
    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="criterion-now"' in body
    assert 'data-now-url="/criterion-now"' in body
    assert "Shock Corridor (1963)" in body
    fragment = admin_client.get("/criterion-now").get_data(as_text=True)
    assert "Shock Corridor (1963)" in fragment
    assert "data-watchlist-scope" in fragment
    fingerprint = re.search(r'data-now-film="([^"]+)"', fragment)
    assert fingerprint and fingerprint.group(1).startswith("Shock Corridor|33667|")
    assert re.search(r"data-now-status>.*Next film around", fragment)
    assert "<html" not in fragment

    # The next film changes the fingerprint. The page swaps the card

    plant("The Naked Kiss", 33669, 90)
    fragment = admin_client.get("/criterion-now").get_data(as_text=True)
    assert "The Naked Kiss (1963)" in fragment
    assert 'data-now-film="The Naked Kiss|33669|' in fragment

    # Subscriber, stale film: the container still renders (a card can
    # appear after the poller stores one). But the fragment is empty

    plant("The Naked Kiss", 33669, -30)
    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="criterion-now"' in body
    assert "On Criterion24/7 now" not in body
    assert admin_client.get("/criterion-now").get_data(as_text=True).strip() == ""
