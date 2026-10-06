"""Test the scan for best-quality files that have no local copy (#274).

The tests cover the scan over the ranking, the fallback detection, the
dead-mount refusal, and the stored report. They also cover the
maintenance pages and the restore and scan buttons. The scan module imports lazily inside each
test. A top-level import would resolve the app singleton before the
test app exists.
"""

import json
import os
import re

from app import db

from tests.conftest import ADMIN_PASSWORD
from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def touch(app, file):
    """Create the local copy of a file row under the test library."""

    path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"x")
    return path


def test_scan_reports_absent_best_files_with_fallbacks(app, monkeypatch):
    from app import missing_files

    monkeypatch.setattr(missing_files, "missing_volumes", lambda config: [])
    with app.app_context():
        # The best copy is absent, and the DVD copy is present. The DVD
        # is the fallback
        served = make_movie("Are You Being Served", 1977)
        best = make_movie_file(served, "WEBRip-1080p", aws_untouched_key="a.mkv")
        touch(app, make_movie_file(served, "DVD"))

        # The best copy is present. Nothing to report, and the absent DVD
        # copy is not a best file
        jaws = make_movie("Jaws", 1975)
        touch(app, make_movie_file(jaws, "Bluray-1080p"))
        make_movie_file(jaws, "DVD")

        # A featurette is its own title group. Its presence is not a
        # fallback for the absent main feature
        evangelion = make_movie("Death and Rebirth", 1997)
        alone = make_movie_file(evangelion, "WEBRip-1080p")
        touch(app, make_movie_file(evangelion, "WEBRip-480p", "Featurettes"))

        # An absent best episode, with a present lower copy
        series = make_tv_series("Columbo")
        episode = make_tv_file(series, 1, 1, "WEBDL-1080p")
        touch(app, make_tv_file(series, 1, 1, "DVD"))
        db.session.commit()
        best_id, alone_id, episode_id = best.id, alone.id, episode.id

        result = missing_files.scan_missing_best_files()

    assert result["missing"] == sorted([best_id, alone_id, episode_id])
    assert result["fallbacks"] == {str(best_id): "DVD", str(episode_id): "DVD"}
    assert result["ranked"] == 5


def test_scan_refuses_to_run_while_a_volume_is_down(app, monkeypatch, caplog):
    from app import missing_files

    monkeypatch.setattr(missing_files, "missing_volumes", lambda config: ["/Volumes/x"])
    app.redis.set(missing_files.MISSING_KEY, json.dumps({"keep": True}))
    with app.app_context():
        assert missing_files.scan_missing_best_files() is None
        missing_files.missing_best_files_task()

    # The previous result stays in place
    assert json.loads(app.redis.get(missing_files.MISSING_KEY)) == {"keep": True}
    assert "not mounted" in caplog.text


def test_task_stores_the_result_and_the_report_loads_the_rows(app, monkeypatch):
    from app import missing_files

    monkeypatch.setattr(missing_files, "missing_volumes", lambda config: [])
    with app.app_context():
        assert missing_files.stored_scan(app.redis) is None
        assert missing_files.missing_best_summary(app.redis) is None
        assert missing_files.missing_best_report(app.redis) is None

        movie = make_movie("Way Out West", 1937)
        absent = make_movie_file(movie, "WEBRip-1080p", aws_untouched_key="w.mkv")
        gone = make_movie_file(make_movie("Deleted Later", 1950), "DVD")
        db.session.commit()

        missing_files.missing_best_files_task()

        summary = missing_files.missing_best_summary(app.redis)
        assert summary["count"] == 2

        # A row deleted after the scan is not in the report
        db.session.delete(gone)
        db.session.commit()
        report = missing_files.missing_best_report(app.redis)

        assert [row["file"].id for row in report["rows"]] == [absent.id]
        row = report["rows"][0]
        assert row["title"] == "Way Out West (1937)"
        assert row["quality"] == "WEBRip-1080p"
        assert row["archived"] is True
        assert row["fallback"] is None


def test_maintenance_pages_show_the_stored_scan(app, admin_client, monkeypatch):
    from app import missing_files

    monkeypatch.setattr(missing_files, "missing_volumes", lambda config: [])

    # Before the first scan, the pages say so
    page = admin_client.get("/maintenance").get_data(as_text=True)
    assert "The first scan has not run yet." in page
    page = admin_client.get("/maintenance/missing").get_data(as_text=True)
    assert "The first scan has not run yet." in page

    with app.app_context():
        movie = make_movie("Way Out West", 1937)
        make_movie_file(
            movie, "WEBRip-1080p", aws_untouched_key="w.mkv", filesize_bytes=2**30
        )
        unarchived = make_movie_file(make_movie("Local Only", 1960), "DVD")
        db.session.commit()
        unarchived_id = unarchived.id
        missing_files.missing_best_files_task()

    page = admin_client.get("/maintenance").get_data(as_text=True)
    assert "Restore missing best copies" in page
    assert 'badge text-bg-light">2<' in page

    page = admin_client.get("/maintenance/missing").get_data(as_text=True)
    assert "Way Out West (1937)" in page
    assert "There are 2 best-quality files with no local copy" in page
    assert ">archived<" in page and ">not archived<" in page
    # The unarchived file has no checkbox. Nothing can restore it
    assert f'name="file_id" value="{unarchived_id}"' not in page

    # The System page carries the alert badge
    page = admin_client.get("/system").get_data(as_text=True)
    assert "2 best copies not local" in page


def test_restore_button_enqueues_a_restore_per_selected_archived_file(
    app, admin_client, monkeypatch
):
    from app import missing_files

    monkeypatch.setattr(missing_files, "missing_volumes", lambda config: [])
    with app.app_context():
        first = make_movie_file(
            make_movie("Way Out West", 1937), "WEBRip-1080p", aws_untouched_key="w.mkv"
        )
        second = make_movie_file(
            make_movie("Sons of the Desert", 1933),
            "WEBRip-1080p",
            aws_untouched_key="s.mkv",
        )
        unarchived = make_movie_file(make_movie("Local Only", 1960), "DVD")
        db.session.commit()
        ids = [str(first.id), str(second.id), str(unarchived.id)]
        missing_files.missing_best_files_task()

    page = admin_client.get("/maintenance/missing").get_data(as_text=True)
    token = csrf_token_from(page)

    # A wrong password requests nothing
    response = admin_client.post(
        "/maintenance/missing",
        data={
            "csrf_token": token,
            "password": "wrong",
            "file_id": ids[:1],
            "missing_restore_submit": "Request restore of selected files",
        },
        follow_redirects=True,
    )
    assert "Incorrect password provided!" in response.get_data(as_text=True)
    assert app.request_queue.count == 0

    response = admin_client.post(
        "/maintenance/missing",
        data={
            "csrf_token": token,
            "password": ADMIN_PASSWORD,
            "file_id": ids,
            "missing_restore_submit": "Request restore of selected files",
        },
        follow_redirects=True,
    )
    assert "Requesting 2 files to be restored" in response.get_data(as_text=True)
    jobs = app.request_queue.jobs
    assert sorted(job.args[0] for job in jobs) == ["s.mkv", "w.mkv"]
    assert {job.func_name for job in jobs} == {"app.videos.aws_restore"}


def test_scan_button_enqueues_the_scan_task(app, admin_client):
    page = admin_client.get("/maintenance/missing").get_data(as_text=True)
    response = admin_client.post(
        "/maintenance/missing",
        data={"csrf_token": csrf_token_from(page), "missing_scan_submit": "Scan now"},
        follow_redirects=True,
    )
    assert "Scanning the library" in response.get_data(as_text=True)
    jobs = app.maintenance_queue.jobs
    assert [job.func_name for job in jobs] == [
        "app.missing_files.missing_best_files_task"
    ]
