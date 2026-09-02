"""Test the Lossy Files report (#212).

The report lists the files with a lossy first audio track and an
available lossless track. It excludes the deliberate trio of the Atmos
pipeline. The E-AC-3 Atmos first track of that trio is exactly as
wanted."""

from app import db
from app.models import FileAudioTrack

from tests.factories import make_movie, make_movie_file


def audio(file, track, codec, compression, fmt=None):
    db.session.add(
        FileAudioTrack(
            file_id=file.id,
            track=track,
            language="eng",
            language_name="English",
            codec=codec,
            format=fmt or codec,
            compression_mode=compression,
        )
    )


def test_lossy_report_excludes_the_atmos_trio(app, admin_client):
    with app.app_context():
        # A lossy first track with a lossless sibling: the order is
        # wrong, or an upgrade was missed. The report lists it.
        wrong = make_movie("Zappa Stand In", 2020)
        wrong_file = make_movie_file(wrong, "Bluray-1080p")
        audio(wrong_file, 1, "Dolby Digital Plus", "Lossy", "E-AC-3")
        audio(wrong_file, 2, "FLAC", "Lossless")

        # The trio of the Atmos pipeline: E-AC-3 Atmos leads by design.
        # The lossless original comes behind it. The report excludes it
        # (#212).
        trio = make_movie("Atmos Trio Film", 2021)
        trio_file = make_movie_file(trio, "Bluray-2160p Remux")
        audio(trio_file, 1, "Dolby Digital Plus with Dolby Atmos", "Lossy", "E-AC-3")
        audio(trio_file, 2, "FLAC", "Lossless")
        audio(trio_file, 3, "Dolby TrueHD with Dolby Atmos", "Lossless", "MLP FBA")

        # An all-lossy file has no lossless track to promote. Thus, it
        # was never on this report. This case is pinned, so the change
        # does not widen the report.
        bare = make_movie("All Lossy Film", 1999)
        bare_file = make_movie_file(bare, "DVD")
        audio(bare_file, 1, "Dolby Digital", "Lossy", "AC-3")

        db.session.commit()

    page = admin_client.get("/library/files?audio=lossy").get_data(as_text=True)
    assert "Zappa Stand In" in page
    assert "Atmos Trio Film" not in page
    assert "All Lossy Film" not in page
