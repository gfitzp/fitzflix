"""Season- and series-level AWS restore fan-out: one aws_restore job per
best-ranked archived file, skipping unarchived and outranked copies.
"""

import re

from app import db

from tests.conftest import ADMIN_PASSWORD
from tests.factories import make_tv_file, make_tv_series


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def build_series(app):
    """A series with a mix of archived, outranked, and unarchived files."""

    series = make_tv_series("Restore Test (2020)")

    # S01E01: two copies; only the best-ranked Bluray should be restored
    make_tv_file(series, 1, 1, "DVD", aws_untouched_key="untouched/e1-dvd.mkv")
    best_e1 = make_tv_file(
        series, 1, 1, "Bluray-1080p", aws_untouched_key="untouched/e1-bluray.mkv"
    )

    # S01E02: best copy was never archived — skipped
    make_tv_file(series, 1, 2, "DVD")

    # S02E01: archived, in another season
    best_s2 = make_tv_file(series, 2, 1, "DVD", aws_untouched_key="untouched/s2e1.mkv")

    db.session.commit()
    return series, best_e1, best_s2


def restore_jobs(app):
    jobs = [
        job
        for job in app.request_queue.jobs
        if job.func_name == "app.videos.aws_restore"
    ]

    # Fan-out restores must use the cheaper Bulk tier the estimate assumes

    assert all(job.kwargs == {"tier": "Bulk"} for job in jobs)
    return [job.args[0] for job in jobs]


def test_season_restore_fans_out_to_best_archived_files(app, admin_client):
    with app.app_context():
        series, best_e1, best_s2 = build_series(app)
        series_id = series.id

    page = admin_client.get(f"/tv/{series_id}/1").get_data(as_text=True)
    assert "Restore season from AWS" in page

    # The cost estimate is shown before anything is submitted

    assert "≈ $" in page

    response = admin_client.post(
        f"/tv/{series_id}/1",
        data={
            "csrf_token": csrf_token_from(page),
            "password": ADMIN_PASSWORD,
            "season_restore_submit": "Restore season from AWS",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Requesting 1 file(s) for season 1" in response.get_data(as_text=True)

    # Only season 1's best archived copy — not the outranked DVD, not the
    # unarchived episode, not season 2

    assert restore_jobs(app) == ["untouched/e1-bluray.mkv"]


def test_series_restore_fans_out_across_seasons(app, admin_client):
    with app.app_context():
        series, best_e1, best_s2 = build_series(app)
        series_id = series.id

    page = admin_client.get(f"/tv/{series_id}").get_data(as_text=True)
    assert "Restore series from AWS" in page

    response = admin_client.post(
        f"/tv/{series_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "password": ADMIN_PASSWORD,
            "series_restore_submit": "Restore series from AWS",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Requesting 2 file(s)" in response.get_data(as_text=True)

    assert sorted(restore_jobs(app)) == [
        "untouched/e1-bluray.mkv",
        "untouched/s2e1.mkv",
    ]


def test_series_restore_does_not_trigger_other_forms(app, admin_client):
    """The restore submit must not fire the transcode or delete handlers."""

    with app.app_context():
        series, _, _ = build_series(app)
        series_id = series.id

    page = admin_client.get(f"/tv/{series_id}").get_data(as_text=True)
    admin_client.post(
        f"/tv/{series_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "password": ADMIN_PASSWORD,
            "series_restore_submit": "Restore series from AWS",
        },
    )

    assert len(app.transcode_queue) == 0
    with app.app_context():
        from app.models import TVSeries

        assert TVSeries.query.filter_by(id=series_id).first() is not None


def test_restore_requires_correct_password(app, admin_client):
    with app.app_context():
        series, _, _ = build_series(app)
        series_id = series.id

    page = admin_client.get(f"/tv/{series_id}").get_data(as_text=True)

    # Wrong password: flash the error, enqueue nothing

    response = admin_client.post(
        f"/tv/{series_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "password": "not-the-password",
            "series_restore_submit": "Restore series from AWS",
        },
        follow_redirects=True,
    )
    assert "Incorrect password provided!" in response.get_data(as_text=True)
    assert restore_jobs(app) == []

    # Missing password: the form fails validation, nothing is enqueued

    admin_client.post(
        f"/tv/{series_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "series_restore_submit": "Restore series from AWS",
        },
    )
    assert restore_jobs(app) == []
