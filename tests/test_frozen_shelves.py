"""The day-frozen shelves (#204): a rail shows the same films in the
same slots all day; a film that stops being eligible mid-day (watched,
waved off) is replaced in its slot and nothing else moves; the next
calendar day starts fresh. Plus daily_shelf, the shared shelf recipe
(Aug 30 2026): urgent rows lead, the rest walk no-repeat quality tiers
into day-stable shuffled slots, and shelves never repeat a film
another shelf on the page already claimed."""

import json

from datetime import date

from app.recommendations import RECS_KEY, daily_shelf, frozen_shelf

from tests.factories import make_movie, make_movie_file


def test_frozen_shelf_replays_and_replaces_in_slot(app):
    calls = []

    def pick():
        calls.append(1)
        return [3, 1, 4, 5]

    eligible = [1, 2, 3, 4, 5, 6, 7]

    first = frozen_shelf(app.redis, 1, "unit", eligible, pick, day="2026-08-26")
    assert first == [3, 1, 4, 5]
    # The replay never re-picks
    again = frozen_shelf(app.redis, 1, "unit", eligible, pick, day="2026-08-26")
    assert again == [3, 1, 4, 5]
    assert len(calls) == 1

    # Film 4 stops being eligible: its slot takes the first eligible id
    # not already showing (2), and every other slot keeps its position

    now_eligible = [1, 2, 3, 5, 6, 7]
    replaced = frozen_shelf(app.redis, 1, "unit", now_eligible, pick, day="2026-08-26")
    assert replaced == [3, 1, 2, 5]

    # The replacement is itself remembered

    assert frozen_shelf(app.redis, 1, "unit", now_eligible, pick, day="2026-08-26") == [
        3,
        1,
        2,
        5,
    ]

    # No replacement left: the slot closes up rather than repeating a
    # shown film

    assert frozen_shelf(app.redis, 1, "unit", [3, 1, 2], pick, day="2026-08-26") == [
        3,
        1,
        2,
    ]

    # A new day starts fresh from pick()

    fresh = frozen_shelf(app.redis, 1, "unit", eligible, pick, day="2026-08-27")
    assert fresh == [3, 1, 4, 5]
    assert len(calls) == 2

    # Shelves and users key separately

    other = frozen_shelf(app.redis, 2, "unit", eligible, lambda: [7], day="2026-08-26")
    assert other == [7]


def test_daily_shelf_urgent_rows_lead_in_order(app):
    """Urgent rows hold the leading slots in the order given — never
    shuffled — and the remaining slots fill from the ranked rows."""

    urgent = [{"id": "u1"}, {"id": "u2"}]
    rows = [{"id": f"r{n}"} for n in range(10)]

    with app.app_context():
        cards = daily_shelf(
            app.redis,
            1,
            "unit-urgent",
            rows,
            set(),
            key=lambda row: row["id"],
            urgent=urgent,
            day=date(2026, 8, 30),
            count=6,
        )

    assert [card["id"] for card in cards[:2]] == ["u1", "u2"]
    assert len(cards) == 6
    assert len({card["id"] for card in cards}) == 6


def test_daily_shelf_never_repeats_across_the_page(app):
    """Two shelves fed the same candidates claim disjoint films: the
    shared `shown` set is the page's no-repeat pool."""

    rows = [{"id": f"r{n}"} for n in range(12)]

    with app.app_context():
        shown = set()
        first = daily_shelf(
            app.redis,
            1,
            "unit-first",
            rows,
            shown,
            key=lambda row: row["id"],
            day=date(2026, 8, 30),
            count=5,
        )
        second = daily_shelf(
            app.redis,
            1,
            "unit-second",
            rows,
            shown,
            key=lambda row: row["id"],
            day=date(2026, 8, 30),
            count=5,
        )

    first_ids = {card["id"] for card in first}
    second_ids = {card["id"] for card in second}
    assert len(first) == 5 and len(second) == 5
    assert not first_ids & second_ids
    assert shown == first_ids | second_ids


def test_daily_shelf_freezes_the_day_and_varies_by_day(app):
    """A frozen shelf replays the same cards all day; the next day
    draws a different arrangement; freeze=False (the ?minutes= lens)
    picks live without writing a snapshot."""

    rows = [{"id": f"r{n}"} for n in range(30)]

    def draw(day, shelf="unit-freeze", freeze=True):
        """One shelf pick for the given day, with a fresh claim set."""

        with app.app_context():
            return [
                card["id"]
                for card in daily_shelf(
                    app.redis,
                    1,
                    shelf,
                    rows,
                    set(),
                    key=lambda row: row["id"],
                    day=day,
                    freeze=freeze,
                )
            ]

    first = draw(date(2026, 8, 30))
    assert len(first) == 12
    assert draw(date(2026, 8, 30)) == first
    assert draw(date(2026, 8, 31)) != first

    # The live lens picks the same day-deterministic result but leaves
    # no snapshot behind

    assert draw(date(2026, 9, 1), shelf="unit-live", freeze=False) == draw(
        date(2026, 9, 1), shelf="unit-live", freeze=False
    )
    assert app.redis.get("fitzflix:shelf:unit-live:1:2026-09-01") is None


def test_landing_rail_is_stable_and_replaces_only_the_watched_slot(app, admin_client):
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.filter_by(admin=True).first().id
        rec_items = []
        titles_by_id = {}
        for n in range(20):
            movie = make_movie(f"Frozen Pick {n:02d}", 1990)
            make_movie_file(movie, "Bluray-1080p")
            rec_items.append({"movie_id": movie.id, "score": 1.0, "because": []})
            titles_by_id[movie.id] = f"Frozen Pick {n:02d} (1990)"
        db.session.commit()

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps({"computed_at": "2026-08-25 01:45", "items": rec_items}),
    )

    def shown_titles():
        body = admin_client.get("/").get_data(as_text=True)
        return [
            title
            for _, title in sorted(
                (body.index(title), title)
                for title in titles_by_id.values()
                if title in body
            )
        ]

    first = shown_titles()
    assert len(first) == 12
    assert shown_titles() == first

    # Watch the film in slot 4: only that slot changes, to a film that
    # wasn't showing, and every other film keeps its position

    watched_title = first[4]
    with app.app_context():
        movie_id = next(
            mid for mid, title in titles_by_id.items() if title == watched_title
        )
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=movie_id,
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()

    second = shown_titles()
    assert len(second) == 12
    assert second[:4] == first[:4]
    assert second[5:] == first[5:]
    assert second[4] != watched_title
    assert second[4] not in first

    # And the replacement sticks on the next render

    assert shown_titles() == second
