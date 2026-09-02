"""Test the runtime-mismatch triage (#234).

The triage finds the files with a bitrate-estimated duration that is
very different from the TMDb runtime of the film. This is the shape of
a title collision at capture time, or of a truncated download. These
tests cover the candidates query, the short-runtime exclusion, the
acknowledgement flow, and the reset on re-import."""

import re

from app import db
from app.models import FileAudioTrack
from app.triage import reset_triage_state, runtime_mismatch_candidates

from tests.factories import make_movie, make_movie_file


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def sized_for(minutes, kbps=10000):
    """Return the filesize_bytes for the given minutes at the given total
    bitrate."""

    return int(minutes * 60 * kbps * 1000 / 8)


def test_candidates_flag_only_hard_mismatches(app):
    with app.app_context():
        # Ratio 1.0: the length agrees. Fitzflix never flags it. The total
        # bitrate is the video bitrate plus the sum of the audio tracks
        fine = make_movie("Agreeable Length", 1990, tmdb_runtime=100)
        fine_file = make_movie_file(
            fine,
            "Bluray-1080p",
            filesize_bytes=sized_for(100),
            video_bitrate_kbps=9000,
        )
        db.session.add(
            FileAudioTrack(
                file_id=fine_file.id,
                track=1,
                language="eng",
                language_name="English",
                bitrate_kbps=1000,
            )
        )

        # 2.5 times the runtime: the 135-minute film is in the record of a
        # 15-minute short. Fitzflix flags it. No audio rows are necessary
        # (coalesce 0)
        overlong = make_movie("Mislabelled Short", 1964, tmdb_runtime=100)
        make_movie_file(
            overlong,
            "WEBDL-1080p",
            filesize_bytes=sized_for(250),
            video_bitrate_kbps=10000,
        )

        # 0.3 times the runtime: the truncated download that Fitzflix
        # imported. Fitzflix flags it
        truncated = make_movie("Broken Download", 1934, tmdb_runtime=100)
        make_movie_file(
            truncated,
            "WEBRip-720p",
            filesize_bytes=sized_for(30),
            video_bitrate_kbps=10000,
        )

        # A 20-minute cartoon in a 60-minute slot is normal. The
        # short-runtime rule excludes it, also at 3 times the runtime
        short = make_movie("Broadcast Slot Cartoon", 1945, tmdb_runtime=20)
        make_movie_file(
            short,
            "SDTV",
            filesize_bytes=sized_for(60),
            video_bitrate_kbps=10000,
        )

        # Fitzflix never compares a featurette with the runtime of the
        # main feature
        featured = make_movie("Extras Carrier", 1963, tmdb_runtime=100)
        make_movie_file(
            featured,
            "DVD",
            feature_type_name="Featurettes",
            filesize_bytes=sized_for(300),
            video_bitrate_kbps=10000,
        )

        # No stored bitrate: there is no data for an estimate
        unprobed = make_movie("Unprobed", 1970, tmdb_runtime=100)
        make_movie_file(unprobed, "DVD", filesize_bytes=sized_for(300))

        db.session.commit()

        flagged = runtime_mismatch_candidates()
        titles = [entry["movie"].title for entry in flagged]
        assert titles == ["Mislabelled Short", "Broken Download"]
        assert round(flagged[0]["ratio"], 2) == 2.5
        assert round(flagged[0]["estimated_minutes"]) == 250
        assert round(flagged[1]["ratio"], 2) == 0.3


def test_acknowledged_files_stay_off_the_list_until_reimport(app):
    with app.app_context():
        movie = make_movie("Full Disc Rip", 1943, tmdb_runtime=32)
        file = make_movie_file(
            movie,
            "DVD",
            filesize_bytes=sized_for(169),
            video_bitrate_kbps=10000,
        )
        db.session.commit()
        assert len(runtime_mismatch_candidates()) == 1

        from datetime import datetime

        file.runtime_mismatch_reviewed = datetime.now()
        db.session.commit()
        assert runtime_mismatch_candidates() == []

        # A re-import deletes the verdict. The length of a replacement is
        # a new length

        reset_triage_state(file)
        db.session.commit()
        assert len(runtime_mismatch_candidates()) == 1


def test_page_lists_and_acknowledges(app, admin_client):
    with app.app_context():
        movie = make_movie("December Seventh", 1943, tmdb_runtime=32)
        file = make_movie_file(
            movie,
            "DVD",
            filesize_bytes=sized_for(169),
            video_bitrate_kbps=10000,
        )
        db.session.commit()
        file_id = file.id

    page = admin_client.get("/maintenance/runtime").get_data(as_text=True)
    assert "December Seventh (1943)" in page
    assert "32m" in page
    assert "169m" in page
    assert "5.3" in page
    assert "verify a flagged file with ffprobe" in page

    # The maintenance card shows the warning colour while the count is
    # not zero

    mpage = admin_client.get("/maintenance").get_data(as_text=True)
    assert re.search(r'btn-warning" href="[^"]*/maintenance/runtime"', mpage)

    response = admin_client.post(
        "/maintenance/runtime",
        data={
            "csrf_token": csrf_token_from(page),
            "file_id": file_id,
            "acknowledge_submit": "Acknowledge",
        },
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "Acknowledged" in body
    assert "nothing to triage" in body

    mpage = admin_client.get("/maintenance").get_data(as_text=True)
    assert re.search(r'btn-secondary" href="[^"]*/maintenance/runtime"', mpage)
