"""The lossy-audio triage (#212): the candidates worklist, the
promote / keep-as-is actions, and the listening-clip comparison's
presentation and envelope correlation (#223)."""

import inspect
import json
import os

from app import db
from app.models import File, FileAudioTrack, FileSubtitleTrack

from tests.factories import make_movie, make_movie_file
from tests.test_subtitle_triage import csrf_token_from


def audio(file, track, codec, compression, fmt=None, streamorder=None, **kwargs):
    row = FileAudioTrack(
        file_id=file.id,
        track=track,
        language="eng",
        language_name="English",
        codec=codec,
        format=fmt or codec,
        compression_mode=compression,
        streamorder=streamorder if streamorder is not None else track,
        **kwargs,
    )
    db.session.add(row)
    db.session.flush()
    return row


def build_candidate(title="Wrong Order", year=2020, container="Matroska"):
    movie = make_movie(title, year)
    file = make_movie_file(movie, "Bluray-1080p", container=container)
    audio(file, 1, "Dolby Digital Plus", "Lossy", "E-AC-3", default=True)
    lossless = audio(file, 2, "DTS-HD Master Audio", "Lossless", "DTS")
    db.session.commit()
    return file, lossless


def test_worklist_matches_the_report_predicates_plus_reviewed(app, admin_client):
    from datetime import datetime

    with app.app_context():
        file, _ = build_candidate()

        # The Atmos pipeline's trio: E-AC-3 Atmos leads deliberately —
        # never a candidate (#212)
        trio = make_movie_file(
            make_movie("Atmos Trio Film", 2021), "Bluray-2160p Remux"
        )
        audio(trio, 1, "Dolby Digital Plus with Dolby Atmos", "Lossy", "E-AC-3")
        audio(trio, 2, "Dolby TrueHD with Dolby Atmos", "Lossless", "MLP FBA")

        # All-lossy: nothing to promote
        bare = make_movie_file(make_movie("All Lossy Film", 1999), "DVD")
        audio(bare, 1, "Dolby Digital", "Lossy", "AC-3")

        # Lossless already leads: nothing to do
        fine = make_movie_file(make_movie("Fine Film", 2001), "Bluray-1080p")
        audio(fine, 1, "FLAC", "Lossless")
        audio(fine, 2, "Dolby Digital", "Lossy", "AC-3")

        # A dismissed candidate stays off the worklist
        kept = make_movie_file(make_movie("Kept Commentary", 2002), "Bluray-1080p")
        audio(kept, 1, "Dolby Digital", "Lossy", "AC-3")
        audio(kept, 2, "FLAC", "Lossless")
        kept.lossy_audio_reviewed = datetime(2026, 8, 29)
        db.session.commit()
        file_id = file.id

    page = admin_client.get("/maintenance/lossy-audio").get_data(as_text=True)
    assert "Wrong Order" in page
    for absent in ("Atmos Trio Film", "All Lossy Film", "Fine Film", "Kept Commentary"):
        assert absent not in page, absent

    # The worklist links each file's own page; the track table lives there

    assert f"/maintenance/lossy-audio/{file_id}" in page
    detail = admin_client.get(f"/maintenance/lossy-audio/{file_id}").get_data(
        as_text=True
    )
    assert "DTS-HD Master Audio" in detail
    assert "Lossy lead" in detail


def test_maintenance_card_counts_candidate_files(app, admin_client):
    import re

    with app.app_context():
        build_candidate()

    page = admin_client.get("/maintenance").get_data(as_text=True)
    assert "Lossy-audio leads" in page
    assert "/maintenance/lossy-audio" in page
    assert re.search(r"Triage lossy audio <span[^>]*>1</span>", page)


def test_dismiss_marks_the_file_reviewed(app, admin_client):
    with app.app_context():
        file, _ = build_candidate()
        file_id = file.id

    page = admin_client.get(f"/maintenance/lossy-audio/{file_id}").get_data(
        as_text=True
    )
    response = admin_client.post(
        "/maintenance/lossy-audio",
        data={
            "csrf_token": csrf_token_from(page),
            "file_id": file_id,
            "dismiss_submit": "Keep as-is",
        },
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "audio as reviewed" in body
    assert "In each file, the first audio track is the best track." in body

    with app.app_context():
        assert db.session.get(File, file_id).lossy_audio_reviewed is not None


def test_promote_enqueues_mkvpropedit_preserving_subtitle_flags(app, admin_client):
    from app.videos import mkvpropedit_task

    with app.app_context():
        file, lossless = build_candidate()
        db.session.add(
            FileSubtitleTrack(
                file_id=file.id,
                track=1,
                language="eng",
                language_name="English",
                elements=1200,
                default=True,
                forced=False,
                format="PGS",
                streamorder=3,
            )
        )
        db.session.add(
            FileSubtitleTrack(
                file_id=file.id,
                track=2,
                language="eng",
                language_name="English",
                elements=40,
                default=False,
                forced=True,
                format="PGS",
                streamorder=4,
            )
        )
        db.session.commit()
        file_id, lossless_track = file.id, lossless.track

        local_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(b"mkv bytes")

    try:
        page = admin_client.get(f"/maintenance/lossy-audio/{file_id}").get_data(
            as_text=True
        )
        response = admin_client.post(
            "/maintenance/lossy-audio",
            data={
                "csrf_token": csrf_token_from(page),
                "file_id": file_id,
                "lossless_track": lossless_track,
                "promote_submit": "Remux with this track in the lead",
            },
            follow_redirects=True,
        )
        assert "in the lead" in response.get_data(as_text=True)

        jobs = [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.mkvpropedit_task"
        ]
        assert len(jobs) == 1
        # The lossless track becomes the default audio (the task's remux
        # pass moves it into the lead); subtitle default and forced
        # flags ride through unchanged
        assert jobs[0].args == (file_id, "2", "1", ["2"])
        inspect.signature(mkvpropedit_task).bind(*jobs[0].args)
    finally:
        os.remove(local_path)


def test_promote_refuses_a_lossy_selection(app, admin_client):
    with app.app_context():
        file, _ = build_candidate()
        file_id = file.id
        local_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(b"mkv bytes")

    try:
        page = admin_client.get(f"/maintenance/lossy-audio/{file_id}").get_data(
            as_text=True
        )
        response = admin_client.post(
            "/maintenance/lossy-audio",
            data={
                "csrf_token": csrf_token_from(page),
                "file_id": file_id,
                "lossless_track": 1,
                "promote_submit": "Remux with this track in the lead",
            },
            follow_redirects=True,
        )
        assert "Select a lossless track" in response.get_data(as_text=True)
        assert app.file_queue.jobs == []
    finally:
        os.remove(local_path)


def test_promote_refuses_non_matroska_and_missing_local(app, admin_client):
    with app.app_context():
        mp4, mp4_lossless = build_candidate(
            title="MP4 Candidate", year=2005, container="MPEG-4"
        )
        gone, gone_lossless = build_candidate(title="Restored Elsewhere", year=2006)
        mp4_id, mp4_track = mp4.id, mp4_lossless.track
        gone_id, gone_track = gone.id, gone_lossless.track

    page = admin_client.get("/maintenance/lossy-audio").get_data(as_text=True)
    assert "MP4 Candidate" in page  # listed on the worklist, badged
    assert "MPEG-4" in page

    detail = admin_client.get(f"/maintenance/lossy-audio/{mp4_id}").get_data(
        as_text=True
    )
    response = admin_client.post(
        "/maintenance/lossy-audio",
        data={
            "csrf_token": csrf_token_from(detail),
            "file_id": mp4_id,
            "lossless_track": mp4_track,
            "promote_submit": "Remux with this track in the lead",
        },
        follow_redirects=True,
    )
    assert "can&#39;t be reordered in place" in response.get_data(as_text=True)

    detail = admin_client.get(f"/maintenance/lossy-audio/{gone_id}").get_data(
        as_text=True
    )
    response = admin_client.post(
        "/maintenance/lossy-audio",
        data={
            "csrf_token": csrf_token_from(detail),
            "file_id": gone_id,
            "lossless_track": gone_track,
            "promote_submit": "Remux with this track in the lead",
        },
        follow_redirects=True,
    )
    assert "not present locally" in response.get_data(as_text=True)
    assert app.file_queue.jobs == []


def test_generate_enqueues_the_comparison_task(app, admin_client):
    with app.app_context():
        file, _ = build_candidate()
        file_id = file.id
        local_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(b"mkv bytes")

    try:
        page = admin_client.get(f"/maintenance/lossy-audio/{file_id}").get_data(
            as_text=True
        )
        assert "Generate listening clips" in page
        response = admin_client.post(
            f"/maintenance/lossy-audio/{file_id}",
            data={
                "csrf_token": csrf_token_from(page),
                "file_id": file_id,
                "generate_submit": "Generate listening clips",
            },
            follow_redirects=True,
        )
        assert "Generating listening clips" in response.get_data(as_text=True)
        jobs = [
            job
            for job in app.transcode_queue.jobs
            if job.func_name == "app.triage.generate_audio_comparison"
        ]
        assert len(jobs) == 1
        assert jobs[0].args == (file_id,)
    finally:
        os.remove(local_path)


def test_import_hook_queues_comparison_only_for_candidates(app):
    from app.triage import maybe_enqueue_audio_comparison

    with app.app_context():
        file, _ = build_candidate()
        trio = make_movie_file(
            make_movie("Atmos Trio Film", 2021), "Bluray-2160p Remux"
        )
        audio(trio, 1, "Dolby Digital Plus with Dolby Atmos", "Lossy", "E-AC-3")
        audio(trio, 2, "Dolby TrueHD with Dolby Atmos", "Lossless", "MLP FBA")
        db.session.commit()

        assert maybe_enqueue_audio_comparison(file.id) is True
        assert maybe_enqueue_audio_comparison(trio.id) is False
        jobs = [
            job
            for job in app.transcode_queue.jobs
            if job.func_name == "app.triage.generate_audio_comparison"
        ]
        assert [job.args for job in jobs] == [(file.id,)]


def test_reimport_resets_the_reviewed_verdict(app):
    from datetime import datetime

    from app.triage import reset_triage_state

    with app.app_context():
        file, _ = build_candidate()
        file.lossy_audio_reviewed = datetime(2026, 8, 29)
        db.session.commit()

        reset_triage_state(file)
        db.session.commit()
        assert file.lossy_audio_reviewed is None


def test_presentation_renders_clocks_percentages_and_verdicts(app):
    from app.triage import audio_comparison_dir, lossy_audio_presentation

    with app.app_context():
        file, _ = build_candidate()
        out_dir = audio_comparison_dir(file.id)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "comparison.json"), "w") as handle:
            json.dump(
                {
                    "pairs": [
                        {
                            "lossy_track": 1,
                            "lossless_track": 2,
                            "correlation": 0.98,
                            "samples": [
                                {
                                    "at": 1200.0,
                                    "lossy": "t1-1.m4a",
                                    "lossless": "t2-1.m4a",
                                    "correlation": 0.96,
                                },
                                {
                                    "at": 3000.0,
                                    "lossy": "t1-2.m4a",
                                    "lossless": "t2-2.m4a",
                                    "correlation": 0.91,
                                },
                                {
                                    "at": 4800.0,
                                    "lossy": "t1-3.m4a",
                                    "lossless": "t2-3.m4a",
                                    "correlation": None,
                                },
                            ],
                        },
                        {
                            "lossy_track": 1,
                            "lossless_track": 3,
                            "samples": [
                                {
                                    "at": 1200.0,
                                    "lossy": "t1-1.m4a",
                                    "lossless": "t3-1.m4a",
                                    "correlation": 0.22,
                                },
                                {
                                    "at": 3000.0,
                                    "lossy": "t1-2.m4a",
                                    "lossless": "t3-2.m4a",
                                    "correlation": 0.31,
                                },
                            ],
                        },
                        {
                            # Full-track verdict outranks flattering
                            # clips: high local numbers, low overall
                            "lossy_track": 1,
                            "lossless_track": 4,
                            "correlation": 0.44,
                            "samples": [
                                {
                                    "at": 1200.0,
                                    "lossy": "t1-1.m4a",
                                    "lossless": "t4-1.m4a",
                                    "correlation": 0.95,
                                },
                            ],
                        },
                    ]
                },
                handle,
            )

        try:
            presented = lossy_audio_presentation(file.id)
        finally:
            import shutil

            shutil.rmtree(out_dir, ignore_errors=True)

    programme, commentary, divergent = presented["pairs"]

    # New shape: the full-track correlation is the verdict and the
    # headline percentage
    assert programme["verdict"] == "match"
    assert programme["percent"] == 98
    assert programme["samples"][0]["clock"] == "0:20:00"
    assert programme["samples"][0]["percent"] == 96
    assert programme["samples"][2]["percent"] is None

    # Old shape (no pair-level correlation): median of the clips
    # decides, with no headline percentage
    assert commentary["verdict"] == "differs"
    assert commentary["percent"] is None

    # Full-track verdict outranks a flattering clip
    assert divergent["verdict"] == "differs"
    assert divergent["percent"] == 44

    with app.app_context():
        assert lossy_audio_presentation(999999) is None


def test_envelope_correlation_separates_programme_from_commentary(app):
    import math
    import random

    from app.triage import _envelope_correlation

    rng = random.Random(212)
    programme = [abs(math.sin(i / 7.0)) * 800 + rng.random() * 40 for i in range(240)]

    # The same loudness contour a few windows late, softer, and with its
    # own codec noise — a lossy encode of the same audio
    lossy_twin = [0.0] * 5 + [value * 0.8 + rng.random() * 40 for value in programme]

    # An unrelated contour — someone talking over the film
    commentary = [
        abs(math.sin(i / 31.0 + 2.0)) * 500 + rng.random() * 200 for i in range(240)
    ]

    assert _envelope_correlation(programme, lossy_twin) > 0.9
    assert _envelope_correlation(programme, commentary) < 0.5
    assert _envelope_correlation([], programme) is None
    assert _envelope_correlation([100.0] * 240, programme) is None  # flat


def test_lossy_triage_requires_admin(user_client):
    assert user_client.get("/maintenance/lossy-audio").status_code == 302
