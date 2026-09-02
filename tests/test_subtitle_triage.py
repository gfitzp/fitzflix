"""The possibly-forced subtitles triage: the same-language sibling-ratio
heuristic, the Library Maintenance card, and the mark-forced / dismiss
actions.
"""

import inspect
import os
import re

from app import db
from app.models import File, FileAudioTrack, FileSubtitleTrack
from tests.factories import make_movie, make_movie_file


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def add_subtitle(
    file,
    track,
    elements,
    language="eng",
    language_name="English",
    forced=False,
    default=False,
    subtitle_format="PGS",
):
    row = FileSubtitleTrack(
        file_id=file.id,
        track=track,
        language=language,
        language_name=language_name,
        elements=elements,
        forced=forced,
        default=default,
        format=subtitle_format,
        streamorder=track,
    )
    db.session.add(row)
    db.session.flush()
    return row


def build_candidate(title="Forced Suspect", year=2000, container="Matroska"):
    movie = make_movie(title, year)
    file = make_movie_file(movie, "Bluray-1080p", container=container)
    add_subtitle(file, 1, 1500, default=True)
    small = add_subtitle(file, 2, 60)
    db.session.commit()
    return file, small


def test_heuristic_flags_small_unforced_same_language_tracks(app, admin_client):
    with app.app_context():
        file, small = build_candidate()

        # None of these should be flagged. "Already Forced" also carries a
        # third small unforced track that would match on ratio alone — a
        # file with a forced track has its forced needs met, so nothing
        # in it is suggested

        already_forced = make_movie_file(make_movie("Already Forced", 2001), "DVD")
        add_subtitle(already_forced, 1, 1500)
        add_subtitle(already_forced, 2, 60, forced=True)
        add_subtitle(already_forced, 3, 45)

        healthy_ratio = make_movie_file(make_movie("Healthy Ratio", 2002), "DVD")
        add_subtitle(healthy_ratio, 1, 1500)
        add_subtitle(healthy_ratio, 2, 400)

        tiny_sibling = make_movie_file(make_movie("Tiny Sibling", 2003), "DVD")
        add_subtitle(tiny_sibling, 1, 80)
        add_subtitle(tiny_sibling, 2, 15)

        empty_track = make_movie_file(make_movie("Empty Track", 2004), "DVD")
        add_subtitle(empty_track, 1, 1500)
        add_subtitle(empty_track, 2, 0)

        cross_language = make_movie_file(make_movie("Cross Language", 2005), "DVD")
        add_subtitle(cross_language, 1, 1500)
        add_subtitle(cross_language, 2, 60, language="fre", language_name="French")
        db.session.commit()
        suspect_id = file.id

    page = admin_client.get("/maintenance/subtitles").get_data(as_text=True)
    assert "Forced Suspect" in page
    # The worklist links each file to its own triage page; the
    # track detail lives there
    assert "60 of 1500" not in page
    detail = admin_client.get(f"/maintenance/subtitles/{suspect_id}").get_data(
        as_text=True
    )
    assert "60 of 1500" in detail
    for absent in (
        "Already Forced",
        "Healthy Ratio",
        "Tiny Sibling",
        "Empty Track",
        "Cross Language",
    ):
        assert absent not in page, absent


def test_maintenance_card_counts_candidate_files(app, admin_client):
    with app.app_context():
        build_candidate()

    page = admin_client.get("/maintenance").get_data(as_text=True)
    assert "Possibly-forced subtitles" in page
    assert "/maintenance/subtitles" in page
    assert re.search(r"Triage subtitle tracks <span[^>]*>1</span>", page)


def test_dismiss_marks_the_file_reviewed(app, admin_client):
    with app.app_context():
        file, _ = build_candidate()
        file_id = file.id

    page = admin_client.get(f"/maintenance/subtitles/{file_id}").get_data(as_text=True)
    response = admin_client.post(
        "/maintenance/subtitles",
        data={
            "csrf_token": csrf_token_from(page),
            "file_id": file_id,
            "dismiss_submit": "Nothing forced here",
        },
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "subtitles as reviewed" in body
    # The flash names the file, so check the empty state rather than
    # the title's absence
    assert "Nothing looks forced right now." in body

    with app.app_context():
        assert db.session.get(File, file_id).subtitle_triage_reviewed is not None


def test_per_file_page_shows_one_file_and_returns_to_origin(app, admin_client):
    """The per-file triage view: only the requested file's
    candidates render, the file page links to it while candidates are
    pending, and actions bounce back to the origin page."""

    with app.app_context():
        file, _ = build_candidate()
        other, _ = build_candidate(title="Other Suspect", year=2001)
        file_id, other_id = file.id, other.id

    # The all-files page lists both and links each file's own page

    body = admin_client.get("/maintenance/subtitles").get_data(as_text=True)
    assert f"/maintenance/subtitles/{file_id}" in body
    assert f"/maintenance/subtitles/{other_id}" in body

    # The per-file page holds only its own file

    body = admin_client.get(f"/maintenance/subtitles/{file_id}").get_data(as_text=True)
    assert "Forced Suspect" in body
    assert "Other Suspect" not in body

    # The file page links to per-file triage while candidates pend

    file_page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
    assert f"/maintenance/subtitles/{file_id}?origin=/file/{file_id}" in (
        file_page.replace("&amp;", "&")
    )

    # Dismissing from the per-file page returns to the origin (the
    # file page), not the triage list

    response = admin_client.post(
        f"/maintenance/subtitles/{file_id}?origin=/file/{file_id}",
        data={
            "csrf_token": csrf_token_from(body),
            "file_id": file_id,
            "dismiss_submit": "Nothing forced here",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/file/{file_id}")

    # Reviewed: the file page link disappears; the other file pends on

    file_page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
    assert f"/maintenance/subtitles/{file_id}?origin=" not in (
        file_page.replace("&amp;", "&")
    )
    with app.app_context():
        assert db.session.get(File, file_id).subtitle_triage_reviewed is not None
        assert db.session.get(File, other_id).subtitle_triage_reviewed is None

    # An off-site origin is never followed

    body = admin_client.get(f"/maintenance/subtitles/{other_id}").get_data(as_text=True)
    response = admin_client.post(
        f"/maintenance/subtitles/{other_id}?origin=https://evil.example",
        data={
            "csrf_token": csrf_token_from(body),
            "file_id": other_id,
            "dismiss_submit": "Nothing forced here",
        },
    )
    assert response.status_code == 302
    assert "evil.example" not in response.headers["Location"]


def test_mark_forced_enqueues_mkvpropedit_preserving_settings(app, admin_client):
    from app.videos import mkvpropedit_task

    with app.app_context():
        file, small = build_candidate()
        db.session.add(
            FileAudioTrack(
                file_id=file.id,
                track=1,
                language="eng",
                language_name="English",
                format="DTS",
                channels="6",
                default=True,
                streamorder=0,
            )
        )
        db.session.commit()
        file_id, small_id = file.id, small.id

        local_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(b"mkv bytes")

    try:
        page = admin_client.get(f"/maintenance/subtitles/{file_id}").get_data(
            as_text=True
        )
        response = admin_client.post(
            "/maintenance/subtitles",
            data={
                "csrf_token": csrf_token_from(page),
                "file_id": file_id,
                "track_ids": [small_id],
                "mark_forced_submit": "Flag selected as forced",
            },
            follow_redirects=True,
        )
        assert "as forced" in response.get_data(as_text=True)

        jobs = [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.mkvpropedit_task"
        ]
        assert len(jobs) == 1
        # Current defaults preserved: audio track 1, default subtitle 1,
        # and the flagged track joins the forced set
        assert jobs[0].args == (file_id, "1", "1", ["2"])
        inspect.signature(mkvpropedit_task).bind(*jobs[0].args)
    finally:
        os.remove(local_path)


def test_mark_forced_refuses_non_matroska(app, admin_client):
    with app.app_context():
        file, small = build_candidate(
            title="MP4 Suspect", year=2006, container="MPEG-4"
        )
        file_id, small_id = file.id, small.id

    page = admin_client.get("/maintenance/subtitles").get_data(as_text=True)
    assert "MP4 Suspect" in page  # listed on the worklist
    assert "MPEG-4" in page
    page = admin_client.get(f"/maintenance/subtitles/{file_id}").get_data(as_text=True)

    response = admin_client.post(
        "/maintenance/subtitles",
        data={
            "csrf_token": csrf_token_from(page),
            "file_id": file_id,
            "track_ids": [small_id],
            "mark_forced_submit": "Flag selected as forced",
        },
        follow_redirects=True,
    )
    assert "cannot edit its subtitle flags in place" in response.get_data(as_text=True)
    assert app.file_queue.jobs == []


def test_subtitle_triage_requires_admin(user_client):
    assert user_client.get("/maintenance/subtitles").status_code == 302


def test_multi_select_flags_all_chosen_tracks_in_one_task(app, admin_client):
    """Two suspected tracks, one checkbox each, one mkvpropedit
    invocation carrying both."""

    with app.app_context():
        file, small = build_candidate(title="Two Suspects", year=2011)
        third = add_subtitle(file, 3, 50)
        db.session.commit()
        file_id, small_id, third_id = file.id, small.id, third.id

        local_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(b"mkv bytes")

    try:
        page = admin_client.get(f"/maintenance/subtitles/{file_id}").get_data(
            as_text=True
        )
        response = admin_client.post(
            "/maintenance/subtitles",
            data={
                "csrf_token": csrf_token_from(page),
                "file_id": file_id,
                "track_ids": [small_id, third_id],
                "mark_forced_submit": "Flag selected as forced",
            },
            follow_redirects=True,
        )
        assert "Marking tracks 2, 3" in response.get_data(as_text=True)

        jobs = [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.mkvpropedit_task"
        ]
        assert len(jobs) == 1
        assert jobs[0].args == (file_id, None, "1", ["2", "3"])
    finally:
        os.remove(local_path)


def test_triage_actions_retire_the_inspection_aids(app, admin_client):
    """Dismissing (or flagging) a file deletes its snapshot directory."""

    from app.triage import triage_snapshot_dir

    with app.app_context():
        file, _ = build_candidate(title="Aids Cleanup", year=2012)
        file_id = file.id
        aids_dir = os.path.join(triage_snapshot_dir(file_id), "2")
        os.makedirs(aids_dir, exist_ok=True)
        with open(os.path.join(aids_dir, "timeline.json"), "w") as f:
            f.write("{}")

    page = admin_client.get(f"/maintenance/subtitles/{file_id}").get_data(as_text=True)
    admin_client.post(
        "/maintenance/subtitles",
        data={
            "csrf_token": csrf_token_from(page),
            "file_id": file_id,
            "dismiss_submit": "Nothing forced here",
        },
    )
    with app.app_context():
        assert not os.path.isdir(triage_snapshot_dir(file_id))


def test_generate_snapshots_and_render_the_aids(app, admin_client, monkeypatch):
    """The generation task writes the timeline and frames from the
    (stubbed) probes, and the triage page renders the density strip,
    cue bounds, and snapshot thumbnails."""

    import app.triage as triage

    with app.app_context():
        file, small = build_candidate(title="Snapshot Subject", year=2013)
        file_id = file.id
        local_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(b"mkv bytes")

    monkeypatch.setattr(triage, "_probe_duration", lambda path: 6000.0)
    monkeypatch.setattr(
        triage,
        "_probe_cue_times",
        lambda path, streamorder: [10.0, 12.0, 15.0, 2500.0, 5900.0],
    )

    def fake_render(path, streamorder, at, out_path):
        """Write a dummy frame."""

        with open(out_path, "wb") as handle:
            handle.write(b"jpg")
        return True

    monkeypatch.setattr(triage, "_render_snapshot", fake_render)

    try:
        assert triage.generate_triage_snapshots(file_id) is True

        with app.app_context():
            aids = triage.triage_presentation(file_id, 2)
        assert aids["cues"] == 5
        assert aids["first"] == "0:00:10"
        assert aids["last"] == "1:38:20"
        assert len(aids["snapshots"]) == 5
        assert max(aids["buckets"]) == 100

        page = admin_client.get(f"/maintenance/subtitles/{file_id}").get_data(
            as_text=True
        )
        assert "5 cues from 0:00:10 to 1:38:20" in page
        assert f"triage/{file_id}/2/snap-1.jpg" in page
        assert 'name="track_ids"' in page

        # Snapshots enlarge in place through the shared modal instead
        # of opening as a new-tab link

        assert "triage-snapshot" in page
        assert "triageSnapshotModal" in page
        assert f'<a href="/static/triage/{file_id}' not in page
    finally:
        os.remove(local_path)
        with app.app_context():
            triage.remove_triage_snapshots(file_id)


def test_import_candidates_enqueue_snapshot_generation(app):
    """The import-time hook queues generation for heuristic matches and
    stays quiet for healthy files."""

    from app.triage import maybe_enqueue_triage_snapshots

    with app.app_context():
        file, _ = build_candidate(title="Enqueue Subject", year=2014)
        file_id = file.id
        healthy = make_movie_file(make_movie("Enqueue Healthy", 2015), "DVD")
        add_subtitle(healthy, 1, 1500)
        add_subtitle(healthy, 2, 900)
        db.session.commit()

        assert maybe_enqueue_triage_snapshots(file_id) is True
        assert maybe_enqueue_triage_snapshots(healthy.id) is False

    jobs = [
        job
        for job in app.transcode_queue.jobs
        if job.func_name == "app.triage.generate_triage_snapshots"
    ]
    assert len(jobs) == 1
    assert jobs[0].args == (file_id,)


def test_deleting_the_local_file_removes_triage_aids(app):
    """delete_local_file covers both deletions and replacements."""

    from app.triage import triage_snapshot_dir

    with app.app_context():
        file, _ = build_candidate(title="Delete Cleanup", year=2016)
        aids_dir = os.path.join(triage_snapshot_dir(file.id), "2")
        os.makedirs(aids_dir, exist_ok=True)
        with open(os.path.join(aids_dir, "timeline.json"), "w") as f:
            f.write("{}")

        file.delete_local_file()
        assert not os.path.isdir(triage_snapshot_dir(file.id))


def test_suspicious_first_track_is_a_candidate(app):
    """A forced-looking track FIRST in the file (Baby Driver's
    [49, 3110, 4334]) is still a candidate: the query baselines on the
    largest same-language sibling, not the first track — so the
    import hook, now gated on this query, generates its aids."""

    from app.triage import forced_subtitle_candidates, maybe_enqueue_triage_snapshots

    with app.app_context():
        file = make_movie_file(make_movie("First Track Suspect", 2017), "DVD")
        add_subtitle(file, 1, 49)
        add_subtitle(file, 2, 3110)
        add_subtitle(file, 3, 4334)
        db.session.commit()

        entries = forced_subtitle_candidates(file_id=file.id)
        assert len(entries) == 1
        assert [e["track"].track for e in entries[0]["tracks"]] == [1]
        assert maybe_enqueue_triage_snapshots(file.id) is True


def test_reset_triage_state_clears_verdict_and_aids(app):
    """A replaced file's earlier dismissal applied to tracks that no
    longer exist (the Wanda case): reset clears the reviewed mark
    and the stale inspection aids so the new content re-earns its way
    off the triage page."""

    from datetime import datetime

    from app.triage import reset_triage_state, triage_snapshot_dir

    with app.app_context():
        file = make_movie_file(make_movie("Reset Subject", 1988), "DVD")
        file.subtitle_triage_reviewed = datetime(2026, 8, 12)
        db.session.commit()

        aid_dir = os.path.join(triage_snapshot_dir(file.id), "2")
        os.makedirs(aid_dir, exist_ok=True)
        with open(os.path.join(aid_dir, "timeline.json"), "w") as fh:
            fh.write("{}")

        reset_triage_state(file)
        db.session.commit()

        assert file.subtitle_triage_reviewed is None
        assert not os.path.isdir(triage_snapshot_dir(file.id))
