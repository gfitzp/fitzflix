"""Test the Criterion 24/7 now-playing card.

These tests cover the parse of the whatsonnow page and of the film
page. The whatsonnow page is a Next.js app since 2026-09-24. It
embeds the schedule as data. The film page has a schema.org block. They also cover the self-scheduling
poller and the card on the landing page. For the card they cover the
gating, the staleness, the star row, and the credits with filmography
links."""

import json
import re

from datetime import datetime, timedelta, timezone


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def whatson_html(title="Shock Corridor", minutes_left=83, guid="8rWb21ax"):
    """Return a now-playing page in the shape of the Next.js app.

    The schedule sits in the data of the app. There, each quotation mark
    is escaped. The current entry ends minutes_left from now. A
    previous entry and a next entry surround it."""

    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now + timedelta(minutes=minutes_left)
    start = end - timedelta(minutes=101)
    stamp = "%Y-%m-%dT%H:%M:%SZ"

    def entry(start, end, title, guid):
        return (
            f'{{\\"startTime\\":\\"{start.strftime(stamp)}\\",'
            f'\\"endTime\\":\\"{end.strftime(stamp)}\\",'
            f'\\"episodeTitle\\":\\"{title}\\",'
            f'\\"shortDescription\\":\\"Film\\",'
            f'\\"longDescription\\":\\"A film \\\\\\"quoted\\\\\\" here.\\",'
            f'\\"guid\\":\\"{guid}\\",\\"duration\\":6060}}'
        )

    schedule = ",".join(
        [
            entry(start - timedelta(minutes=90), start, "Black Girl", "aaaa0000"),
            entry(start, end, title, guid),
            entry(end, end + timedelta(minutes=95), "Stagecoach", "bbbb1111"),
        ]
    )
    return f"""
<h1 class="Hero-module-less-module__epcjfa__title">{title}</h1>
<a href="https://www.criterionchannel.com/live/1emmgvqX/criterion-24-7">Watch Live</a>
<a href="/films/{guid}/shock-corridor">Film Page</a>
<script>self.__next_f.push([1,"{{\\"deeplink\\":\\"https://www.criterionchannel.com/live/1emmgvqX/criterion-24-7\\",\\"schedule\\":[{schedule}]}}"])</script>
"""


WHATSON_HTML = whatson_html()

INFO_HTML = """
<script type="application/ld+json">{"@context":"https://schema.org","@type":"VideoObject","name":"Shock Corridor","director":[{"@type":"Person","name":"Samuel Fuller"}],"actor":[{"@type":"Person","name":"Peter Breck"},{"@type":"Person","name":"Constance Towers"},{"@type":"Person","name":"Gene Evans"}]}</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Movie","name":"Shock Corridor","datePublished":"1963-09-11","actor":[{"@type":"Person","name":"Peter Breck"},{"@type":"Person","name":"Constance Towers"},{"@type":"Person","name":"Gene Evans"}],"countryOfOrigin":[{"@type":"Country","name":"United States"}]}</script>
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


def test_parse_whatson_page_reads_title_link_and_schedule(app):
    from app.criterion_now import parse_watch_live_url, parse_whatson_page

    parsed = parse_whatson_page(WHATSON_HTML)
    assert parsed["title"] == "Shock Corridor"
    assert (
        parsed["more_url"]
        == "https://www.criterionchannel.com/films/8rWb21ax/shock-corridor"
    )
    left = parsed["ends_at"] - datetime.now(timezone.utc)
    assert timedelta(minutes=82) < left <= timedelta(minutes=83)
    assert parsed["ends_at"] - parsed["starts_at"] == timedelta(minutes=101)

    # The films after the current 1, with the short film link by id
    assert [entry["title"] for entry in parsed["upcoming"]] == ["Stagecoach"]
    assert parsed["upcoming"][0]["more_url"] == (
        "https://www.criterionchannel.com/films/bbbb1111"
    )
    assert parsed["upcoming"][0]["starts_at"] == parsed["ends_at"].strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert parsed["stale"] is False

    assert (
        parse_watch_live_url(WHATSON_HTML)
        == "https://www.criterionchannel.com/live/1emmgvqX/criterion-24-7"
    )

    # The title comes from the heading. The end time comes from the
    # schedule entry with that title. At a film boundary, the heading
    # can name the next film before the clock enters its window. Then
    # the end time of that next entry wins
    parsed = parse_whatson_page(
        whatson_html(minutes_left=1).replace(
            '<h1 class="Hero-module-less-module__epcjfa__title">Shock Corridor',
            '<h1 class="Hero-module-less-module__epcjfa__title">Stagecoach',
        )
    )
    assert parsed["title"] == "Stagecoach"
    left = parsed["ends_at"] - datetime.now(timezone.utc)
    assert timedelta(minutes=95) < left <= timedelta(minutes=96)
    assert parsed["upcoming"] == []

    # A later showing that starts well after now is a rerun, not the
    # film that is on. It gives no end time.
    parsed = parse_whatson_page(
        whatson_html(minutes_left=83).replace(
            '<h1 class="Hero-module-less-module__epcjfa__title">Shock Corridor',
            '<h1 class="Hero-module-less-module__epcjfa__title">Stagecoach',
        )
    )
    assert parsed["ends_at"] is None
    assert parsed["stale"] is False

    # A heading that is in no schedule entry gives no end time. Fitzflix
    # never guesses. The 1st film link on the page is still the film.
    # The upcoming films are the films that start after now
    parsed = parse_whatson_page(
        whatson_html().replace(
            '<h1 class="Hero-module-less-module__epcjfa__title">Shock Corridor',
            '<h1 class="Hero-module-less-module__epcjfa__title">Mystery Film',
        )
    )
    assert parsed["title"] == "Mystery Film"
    assert (
        parsed["more_url"]
        == "https://www.criterionchannel.com/films/8rWb21ax/shock-corridor"
    )
    assert parsed["ends_at"] is None
    assert [entry["title"] for entry in parsed["upcoming"]] == ["Stagecoach"]

    assert parse_whatson_page("<html>redesigned</html>") is None


def test_schedule_entries_without_a_guid_stay_separate(app):
    """Test that an entry with no guid does not swallow the next entry.

    Some schedule entries have no guid (Point of Order!, 2026-09-24).
    A regex over the escaped data ran the title of such an entry into
    the next entry. Then the next film dropped out of the schedule."""

    from app.criterion_now import _schedule_entries, parse_whatson_page

    page = (
        whatson_html()
        .replace('\\"guid\\":\\"aaaa0000\\",', "")
        .replace('\\"guid\\":\\"bbbb1111\\",', "")
    )
    assert "aaaa0000" not in page and "bbbb1111" not in page

    entries = _schedule_entries(page)
    assert [(title, guid) for _, _, title, guid in entries] == [
        ("Black Girl", None),
        ("Shock Corridor", "8rWb21ax"),
        ("Stagecoach", None),
    ]

    parsed = parse_whatson_page(page)
    assert parsed["ends_at"] is not None
    assert parsed["upcoming"][0]["title"] == "Stagecoach"
    assert parsed["upcoming"][0]["more_url"] is None


def test_stale_heading_keeps_the_stored_film_and_retries(app, monkeypatch):
    """Test that a heading of a film that just ended changes nothing.

    The page is a cached render. Just after a film ends, it can still
    show that film. The poll must not store the old film with no end
    time. It tries again after STALE_RETRY."""

    import app.criterion_now as criterion_now

    # Black Girl ended 11 minutes ago. Shock Corridor is on.
    page = whatson_html(minutes_left=90).replace(
        '<h1 class="Hero-module-less-module__epcjfa__title">Shock Corridor',
        '<h1 class="Hero-module-less-module__epcjfa__title">Black Girl',
    )
    parsed = criterion_now.parse_whatson_page(page)
    assert parsed["stale"] is True
    assert parsed["ends_at"] is None

    def fake_requests_get(url, timeout=None):
        class FakeResponse:
            text = page

            def raise_for_status(self):
                """Never an HTTP error."""

        return FakeResponse()

    monkeypatch.setattr(criterion_now.requests, "get", fake_requests_get)
    with app.app_context():
        app.redis.set(criterion_now.NOW_KEY, json.dumps({"title": "Kept Film"}))
        assert criterion_now.poll_criterion_now() is True
        assert json.loads(app.redis.get(criterion_now.NOW_KEY)) == {
            "title": "Kept Film"
        }
        registry = app.maintenance_queue.scheduled_job_registry
        booked = registry.get_scheduled_time(criterion_now.POLL_JOB_ID)
        wait = booked.astimezone() - datetime.now(timezone.utc)
        assert timedelta(seconds=30) < wait <= criterion_now.STALE_RETRY


def test_stamps_are_utc_and_legacy_stamps_still_read(app):
    """Test the stored time format.

    Redis holds UTC stamps. A local stamp repeats in the hour when the
    clocks go back. The reader still takes the local stamps of older
    polls."""

    from app.criterion_now import _parse_stamp, _stamp

    moment = datetime(2026, 11, 1, 6, 10, tzinfo=timezone.utc)
    assert _stamp(moment) == "2026-11-01T06:10:00Z"
    assert _parse_stamp("2026-11-01T06:10:00Z") == moment

    local = moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert _parse_stamp(local).tzinfo is not None


def test_parse_film_info_reads_the_schema_block(app):
    """Test that the film info merges the Movie and VideoObject blocks.

    The live page of The Great Dictator (2026-09-24) names the director
    only in the VideoObject block. The Movie block has the date and the
    country. Each field takes the 1st block that has it."""

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


def test_parse_whatson_page_flattens_nonbreaking_spaces(app):
    from app.criterion_now import parse_whatson_page

    parsed = parse_whatson_page(
        WHATSON_HTML.replace("Shock Corridor", "Shock&amp;nbsp;Corridor")
    )
    assert parsed["title"] == "Shock Corridor"


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
    ends_at = datetime.strptime(stored["ends_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    left = ends_at - datetime.now(timezone.utc)
    assert timedelta(minutes=82) < left <= timedelta(minutes=83)
    starts_at = datetime.strptime(stored["starts_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    assert ends_at - starts_at == timedelta(minutes=101)

    # The upcoming films are stored with the same enrichment as the
    # current 1
    schedule = json.loads(app.redis.get(criterion_now.SCHEDULE_KEY))
    assert [entry["title"] for entry in schedule["upcoming"]] == ["Stagecoach"]
    assert schedule["upcoming"][0]["tmdb_id"] == 33667
    assert schedule["upcoming"][0]["director"] == "Samuel Fuller"
    assert schedule["upcoming"][0]["starts_at"] == stored["ends_at"]

    # Fitzflix schedules the next poll POLL_CUSHION after the end time,
    # to the second. There is only 1 poll, under the deterministic job id

    with app.app_context():
        registry = app.maintenance_queue.scheduled_job_registry
        assert criterion_now.POLL_JOB_ID in registry.get_job_ids()
        booked = registry.get_scheduled_time(criterion_now.POLL_JOB_ID)
        expected = ends_at + criterion_now.POLL_CUSHION
        assert abs((booked.astimezone() - expected).total_seconds()) < 2

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
    assert "On Criterion 24/7 now" not in body

    subscribe_criterion(app)
    body = admin_client.get("/").get_data(as_text=True)
    assert "On Criterion 24/7 now" in body
    assert "Shock Corridor (1963)" in body
    assert "Directed by Samuel Fuller" in body
    assert "Starring Peter Breck" in body
    assert "/shock.jpg" in body
    assert "Next film around" in body
    assert "criterionchannel.com/live/1emmgvqX/criterion-24-7" in body

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
    assert "On Criterion 24/7 now" not in body


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
    assert "On Criterion 24/7 now" in body
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
    assert "On Criterion 24/7 now" not in body
    assert admin_client.get("/criterion-now").get_data(as_text=True).strip() == ""


def test_card_turns_over_from_the_stored_schedule(app, admin_client):
    """Test that the card shows the next film at its start time.

    The poller stores the upcoming films with the current 1. After the
    end time of the current film, the card takes the upcoming entry
    whose window contains now. It does not wait for the next poll. The
    entry after that 1 becomes the preview. A schedule older than
    SCHEDULE_TRUST is not used. Then the card hides, as before."""

    import app.criterion_now as criterion_now

    subscribe_criterion(app)
    stamp = "%Y-%m-%d %H:%M:%S"

    def at(minutes):
        return (datetime.now() + timedelta(minutes=minutes)).strftime(stamp)

    app.redis.set(
        criterion_now.NOW_KEY,
        json.dumps(
            {
                "title": "Shock Corridor",
                "year": 1963,
                "tmdb_id": 33667,
                "poster_path": "/shock.jpg",
                "watch_url": "https://www.criterionchannel.com/live/1emmgvqX/criterion-24-7",
                "starts_at": at(-103),
                "ends_at": at(-2),
            }
        ),
    )
    upcoming = [
        {
            "title": "Stagecoach",
            "year": 1939,
            "director": "John Ford",
            "tmdb_id": None,
            "poster_path": None,
            "more_url": "https://www.criterionchannel.com/films/bbbb1111",
            "starts_at": at(-2),
            "ends_at": at(93),
        },
        {
            "title": "The Hero",
            "more_url": "https://www.criterionchannel.com/films/cccc2222",
            "starts_at": at(93),
            "ends_at": at(200),
        },
    ]
    app.redis.set(
        criterion_now.SCHEDULE_KEY,
        json.dumps({"fetched_at": at(-60), "upcoming": upcoming}),
    )

    body = admin_client.get("/").get_data(as_text=True)
    assert "Stagecoach (1939)" in body
    assert "Directed by John Ford" in body
    assert "Shock Corridor" not in body
    assert "About 2 minutes in" in body
    assert ">Up next<" in body
    assert "The Hero" in body
    assert "criterionchannel.com/films/cccc2222" in body
    assert 'title="The Hero"' in body
    assert "data-now-next" in body
    assert 'data-now-ends="' in body

    # The fragment carries the same turned-over film
    fragment = admin_client.get("/criterion-now").get_data(as_text=True)
    assert "Stagecoach (1939)" in fragment

    # An old schedule is not trusted. The stored film is over. Thus,
    # within STALE_GRACE the old film still shows, and no preview
    app.redis.set(
        criterion_now.SCHEDULE_KEY,
        json.dumps({"fetched_at": at(-7 * 60), "upcoming": upcoming}),
    )
    body = admin_client.get("/").get_data(as_text=True)
    assert "Shock Corridor (1963)" in body
    assert ">Up next<" not in body


def test_turnover_prefers_the_film_that_is_on(app):
    """Test that the film that is on wins over the film that just ended.

    A failed poll at a boundary leaves the stored film 2 entries back.
    The entry that ended within STALE_GRACE also meets the grace test.
    The entry whose window contains now must still win."""

    from app.criterion_now import _stamp, _turned_over

    now = datetime.now(timezone.utc)

    def at(minutes):
        return _stamp(now + timedelta(minutes=minutes))

    stored = {"title": "Shock Corridor", "ends_at": at(-100)}
    upcoming = [
        {"title": "Stagecoach", "starts_at": at(-100), "ends_at": at(-5)},
        {"title": "The Hero", "starts_at": at(-5), "ends_at": at(90)},
        {"title": "Black Girl", "starts_at": at(90), "ends_at": at(160)},
    ]
    film, rest = _turned_over(stored, upcoming, now)
    assert film["title"] == "The Hero"
    assert [entry["title"] for entry in rest] == ["Black Girl"]

    # With no entry on now, the film that just ended stays on the card
    # through the grace.
    film, rest = _turned_over(stored, upcoming[:1], now)
    assert film["title"] == "Stagecoach"


def fake_feed(monkeypatch, criterion_now, fetched):
    """Serve the now-playing page and the film pages. Record each film fetch."""

    def fake_requests_get(url, timeout=None):
        if "whatsonnow" not in url:
            fetched.append(url)

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


def test_poll_books_the_next_poll_before_the_lookups(app, monkeypatch):
    """Test that a run that dies in the enrichment keeps the chain (#267).

    The next poll is booked for the end of the film before any film
    page is fetched. A failure after that point does not lose it."""

    import app.criterion_now as criterion_now

    fake_feed(monkeypatch, criterion_now, [])
    booked_first = []

    def dies(title, more_url, known=None):
        registry = app.maintenance_queue.scheduled_job_registry
        booked_first.append(criterion_now.POLL_JOB_ID in registry.get_job_ids())
        raise RuntimeError("the job ran out of time")

    monkeypatch.setattr(criterion_now, "_enriched_entry", dies)
    assert criterion_now.poll_criterion_now() is True
    assert booked_first == [True]

    with app.app_context():
        registry = app.maintenance_queue.scheduled_job_registry
        booked = registry.get_scheduled_time(criterion_now.POLL_JOB_ID)
    wait = booked.astimezone() - datetime.now(timezone.utc)
    # The film ends in 83 minutes. The booking is not the 30-minute retry.
    assert timedelta(minutes=82) < wait <= timedelta(minutes=84)
    assert registry.get_job_ids().count(criterion_now.POLL_JOB_ID) == 1


def test_poll_reuses_the_enrichment_of_the_last_poll(app, monkeypatch):
    """Test that a film enriched by the last poll is not fetched again.

    The first poll fetches the film page of the current film and of the
    upcoming film. The second poll fetches none. A stored enrichment
    that holds nothing is tried again."""

    import app.criterion_now as criterion_now

    fetched = []
    fake_feed(monkeypatch, criterion_now, fetched)
    assert criterion_now.poll_criterion_now() is True
    assert len(fetched) == 2

    fetched.clear()
    assert criterion_now.poll_criterion_now() is True
    assert fetched == []
    stored = json.loads(app.redis.get(criterion_now.NOW_KEY))
    assert stored["director"] == "Samuel Fuller"
    assert stored["tmdb_id"] == 33667

    # An empty enrichment is a failure. The next poll fetches it again.
    stored.update({field: None for field in criterion_now.ENRICHED_FIELDS})
    app.redis.set(criterion_now.NOW_KEY, json.dumps(stored))
    app.redis.delete(criterion_now.SCHEDULE_KEY)
    assert criterion_now.poll_criterion_now() is True
    assert len(fetched) == 2


def test_up_next_posters_carry_the_poster_popover(app, admin_client):
    """Test that a matched Up next film opens the poster card of the site.

    The poster links to its card by TMDB id and drops the title
    attribute, which Bootstrap would show as the head of the card. A
    film with no TMDB match keeps its plain link and its title. The live
    refresh closes an open card before it replaces the posters."""

    import app.criterion_now as criterion_now

    subscribe_criterion(app)
    now = datetime.now(timezone.utc)

    def at(minutes):
        return criterion_now._stamp(now + timedelta(minutes=minutes))

    app.redis.set(
        criterion_now.NOW_KEY,
        json.dumps(
            {"title": "Shock Corridor", "starts_at": at(-10), "ends_at": at(90)}
        ),
    )
    upcoming = [
        {
            "title": "Stagecoach",
            "year": 1939,
            "tmdb_id": 995,
            "more_url": "https://www.criterionchannel.com/films/bbbb1111",
            "starts_at": at(90),
            "ends_at": at(186),
        },
        {
            "title": "The Hero",
            "more_url": "https://www.criterionchannel.com/films/cccc2222",
            "starts_at": at(186),
            "ends_at": at(300),
        },
    ]
    app.redis.set(
        criterion_now.SCHEDULE_KEY,
        json.dumps({"fetched_at": at(0), "upcoming": upcoming}),
    )

    body = admin_client.get("/").get_data(as_text=True)
    assert 'data-card-url="/movie_card?tmdb_id=995"' in body
    assert 'title="Stagecoach (1939)"' not in body
    # Stagecoach has no poster. The link still names the film.
    assert 'aria-label="Stagecoach (1939)"' in body
    assert 'title="The Hero"' in body
    assert body.count('addEventListener("fitzflix:card-hide"') == 1
    # The refresh names its own card. A card open on another shelf stays.
    assert (
        body.count('new CustomEvent("fitzflix:card-hide", {detail: {within: holder}})')
        == 1
    )
    assert "within.contains(anchor)" in body


def test_page_stale_past_the_grace_never_stores_the_old_film(app, monkeypatch):
    """Test a page that stays stale for longer than STALE_GRACE.

    The old heading must not become the current film with no end time.
    The poll keeps waiting, at the slower STALE_BACKOFF. A heading of a
    film that ended more than STALE_LIMIT ago is not read as stale."""

    import app.criterion_now as criterion_now

    # Black Girl ended 21 minutes ago. Shock Corridor is on.
    page = whatson_html(minutes_left=80).replace(
        '__title">Shock Corridor', '__title">Black Girl'
    )
    parsed = criterion_now.parse_whatson_page(page)
    assert parsed["stale"] is True and parsed["ends_at"] is None

    def fake_requests_get(url, timeout=None):
        class FakeResponse:
            text = page

            def raise_for_status(self):
                """Never an HTTP error."""

        return FakeResponse()

    monkeypatch.setattr(criterion_now.requests, "get", fake_requests_get)
    with app.app_context():
        app.redis.set(criterion_now.NOW_KEY, json.dumps({"title": "Kept Film"}))
        assert criterion_now.poll_criterion_now() is True
        assert json.loads(app.redis.get(criterion_now.NOW_KEY)) == {
            "title": "Kept Film"
        }
        booked = app.maintenance_queue.scheduled_job_registry.get_scheduled_time(
            criterion_now.POLL_JOB_ID
        )
    wait = booked.astimezone() - datetime.now(timezone.utc)
    assert criterion_now.STALE_RETRY < wait <= criterion_now.STALE_BACKOFF

    # Past STALE_LIMIT, the heading counts as a title in no entry.
    later = datetime.now(timezone.utc) + criterion_now.STALE_LIMIT
    parsed = criterion_now.parse_whatson_page(page, later)
    assert parsed["stale"] is False


def test_failed_tmdb_call_is_enriched_again(app, monkeypatch):
    """Test that a transient TMDB failure is not kept by the reuse.

    The film page gave a year and a director, but the TMDB details
    failed. The entry is marked retry. The next poll enriches it again
    and gets the poster."""

    import app.criterion_now as criterion_now

    fetched = []
    fake_feed(monkeypatch, criterion_now, fetched)
    good = criterion_now.enriched_movie
    monkeypatch.setattr(criterion_now, "enriched_movie", lambda tmdb_id: None)
    assert criterion_now.poll_criterion_now() is True
    stored = json.loads(app.redis.get(criterion_now.NOW_KEY))
    assert stored["tmdb_id"] is None and stored["retry"] is True

    fetched.clear()
    monkeypatch.setattr(criterion_now, "enriched_movie", good)
    assert criterion_now.poll_criterion_now() is True
    assert len(fetched) == 2
    stored = json.loads(app.redis.get(criterion_now.NOW_KEY))
    assert stored["tmdb_id"] == 33667 and "retry" not in stored
