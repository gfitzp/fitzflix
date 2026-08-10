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

        # None of these should be flagged:
        already_forced = make_movie_file(make_movie("Already Forced", 2001), "DVD")
        add_subtitle(already_forced, 1, 1500)
        add_subtitle(already_forced, 2, 60, forced=True)

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

    page = admin_client.get("/maintenance/subtitles").get_data(as_text=True)
    assert "Forced Suspect" in page
    assert "60 of 1500" in page
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

    page = admin_client.get("/maintenance/subtitles").get_data(as_text=True)
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
        page = admin_client.get("/maintenance/subtitles").get_data(as_text=True)
        response = admin_client.post(
            "/maintenance/subtitles",
            data={
                "csrf_token": csrf_token_from(page),
                "track_id": small_id,
                "mark_forced_submit": "Mark forced",
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
        small_id = small.id

    page = admin_client.get("/maintenance/subtitles").get_data(as_text=True)
    assert "MP4 Suspect" in page  # listed, but without a mark button
    assert "MPEG-4" in page

    response = admin_client.post(
        "/maintenance/subtitles",
        data={
            "csrf_token": csrf_token_from(page),
            "track_id": small_id,
            "mark_forced_submit": "Mark forced",
        },
        follow_redirects=True,
    )
    assert "can&#39;t be edited in place" in response.get_data(as_text=True)
    assert app.file_queue.jobs == []


def test_subtitle_triage_requires_admin(user_client):
    assert user_client.get("/maintenance/subtitles").status_code == 302
