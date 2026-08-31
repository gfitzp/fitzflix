"""The TMDB change-driven refresh sweep: change-feed enumeration,
library intersection, the ended/canceled TV filter, and the
watermark's advance/hold semantics."""

from datetime import datetime, timedelta, timezone

from tests.factories import make_movie, make_tv_series
from tests.test_leaving_criterion import FakeResponse


def changes_page(ids, total_pages=1):
    return FakeResponse(
        payload={
            "results": [{"id": i, "adult": False} for i in ids],
            "page": 1,
            "total_pages": total_pages,
            "total_results": len(ids),
        }
    )


def sweep_with_feeds(app, monkeypatch, movie_pages=None, tv_pages=None):
    """Run the sweep against canned change feeds; page lists are per
    media type and served identically for every day slice (the id sets
    dedup). Returns (result, requests seen)."""

    import app.tmdb_changes as tc

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    seen = []

    def fake_tmdb_get(url, params=None, **kwargs):
        seen.append((url, dict(params)))
        pages = movie_pages if url.endswith("/movie/changes") else tv_pages
        pages = pages or [[]]
        page = params["page"]
        if page <= len(pages):
            return changes_page(pages[page - 1], total_pages=len(pages))
        return changes_page([], total_pages=len(pages))

    monkeypatch.setattr(tc, "tmdb_get", fake_tmdb_get)
    return tc.refresh_changed_records(), seen


def test_only_changed_unignored_movies_are_queued(app, monkeypatch):
    from app import db

    with app.app_context():
        jaws = make_movie("Jaws", 1975, tmdb_id=578)
        make_movie("Gandhi", 1982, tmdb_id=783)
        make_movie("1982 Secret", 1982, tmdb_id=111, tmdb_ignored=True)
        make_movie("1990 Fileless", 1990)
        db.session.commit()
        jaws_id = jaws.id

    # The changed feed spans two pages; the ignored movie's id and an
    # id outside the library both appear and must queue nothing

    queued, seen = sweep_with_feeds(
        app, monkeypatch, movie_pages=[[578, 999999], [111]]
    )

    assert queued == 1
    assert [job.args for job in app.request_queue.jobs] == [("Movies", jaws_id, 578)]
    assert max(p["page"] for url, p in seen if "/movie/" in url) == 2

    import app.tmdb_changes as tc

    assert app.redis.get(tc.LAST_RUN_KEY) is not None


def test_tv_takes_only_what_the_in_production_sweep_leaves(app, monkeypatch):
    """The 3:45 sweep already re-fetches everything except ended and
    canceled series; this sweep must cover exactly the complement, so a
    changed id queues only when the series is ended/canceled and not
    ignored."""

    from app import db

    with app.app_context():
        mash = make_tv_series("M*A*S*H", tmdb_id=918, tmdb_status="Ended")
        make_tv_series(
            "Strange New Worlds",
            tmdb_id=103516,
            tmdb_status="Returning Series",
            tmdb_in_production=True,
        )
        make_tv_series("Never Refreshed", tmdb_id=555)
        make_tv_series(
            "Baltimore Orioles", tmdb_id=666, tmdb_status="Ended", tmdb_ignored=True
        )
        db.session.commit()
        mash_id = mash.id

    queued, _ = sweep_with_feeds(app, monkeypatch, tv_pages=[[918, 103516, 555, 666]])

    assert queued == 1
    assert [job.args for job in app.request_queue.jobs] == [("TV Shows", mash_id, 918)]


def test_window_is_day_sliced_and_clamped_to_the_lookback(app, monkeypatch):
    """A watermark beyond TMDB's 14-day change history clamps to the
    cap (older edits are unknowable), and the window is enumerated in
    one-day slices so no slice can approach the 500-page ceiling."""

    import app.tmdb_changes as tc

    now = datetime.now(timezone.utc)
    app.redis.set(tc.LAST_RUN_KEY, (now - timedelta(days=30)).isoformat())

    _, seen = sweep_with_feeds(app, monkeypatch)

    starts = {p["start_date"] for _, p in seen}
    assert (
        min(starts) == (now - timedelta(days=tc.LOOKBACK_CAP_DAYS)).date().isoformat()
    )
    assert len(starts) == tc.LOOKBACK_CAP_DAYS + 1
    for _, p in seen:
        start = datetime.fromisoformat(p["start_date"])
        end = datetime.fromisoformat(p["end_date"])
        assert end - start == timedelta(days=1)


def test_failed_enumeration_keeps_the_watermark(app, monkeypatch):
    """A cut-short read must not advance the watermark: the next sweep
    re-covers the window, and a doubly-refreshed title is harmless
    where a silently missed edit is not."""

    import app.tmdb_changes as tc

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    stamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    app.redis.set(tc.LAST_RUN_KEY, stamp)

    def failing_tmdb_get(url, params=None, **kwargs):
        raise ConnectionError("TMDB unreachable")

    monkeypatch.setattr(tc, "tmdb_get", failing_tmdb_get)

    assert tc.refresh_changed_records() == 0
    assert app.redis.get(tc.LAST_RUN_KEY).decode() == stamp


def test_sweep_without_an_api_key_is_a_noop(app):
    import app.tmdb_changes as tc

    assert not app.config["TMDB_API_KEY"]
    assert tc.refresh_changed_records() == 0
    assert app.request_queue.count == 0


def test_cron_table_sweeps_tmdb_changes_nightly(app):
    """The sweep sits in the nightly TMDB-heavy window, adjacent to the
    in-production TV refresh whose coverage it excludes."""

    from app import cron_table

    with app.app_context():
        entries = {entry["func"]: entry for entry in cron_table(app.config)}

    entry = entries["app.tmdb_changes.refresh_changed_records"]
    assert entry["cron"] == "35 3 * * *"
    assert entry["queue"] == "fitzflix-maintenance"
