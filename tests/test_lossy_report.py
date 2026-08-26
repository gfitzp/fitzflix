"""The Lossy Files report (#212): files with a lossy first audio track
and a lossless track available — minus the Atmos pipeline's deliberate
trio, whose E-AC-3 Atmos first track is exactly as wanted."""

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
        # Lossy first track with a lossless sibling: the wrong order
        # (or a missed upgrade) — listed
        wrong = make_movie("Zappa Stand In", 2020)
        wrong_file = make_movie_file(wrong, "Bluray-1080p")
        audio(wrong_file, 1, "Dolby Digital Plus", "Lossy", "E-AC-3")
        audio(wrong_file, 2, "FLAC", "Lossless")

        # The Atmos pipeline's trio: E-AC-3 Atmos leads deliberately,
        # the lossless original rides behind — excluded (#212)
        trio = make_movie("Atmos Trio Film", 2021)
        trio_file = make_movie_file(trio, "Bluray-2160p Remux")
        audio(trio_file, 1, "Dolby Digital Plus with Dolby Atmos", "Lossy", "E-AC-3")
        audio(trio_file, 2, "FLAC", "Lossless")
        audio(trio_file, 3, "Dolby TrueHD with Dolby Atmos", "Lossless", "MLP FBA")

        # All-lossy file: no lossless to promote, so it has never been
        # on this report — pinned so the change doesn't widen it
        bare = make_movie("All Lossy Film", 1999)
        bare_file = make_movie_file(bare, "DVD")
        audio(bare_file, 1, "Dolby Digital", "Lossy", "AC-3")

        db.session.commit()

    page = admin_client.get("/library/files?audio=lossy").get_data(as_text=True)
    assert "Zappa Stand In" in page
    assert "Atmos Trio Film" not in page
    assert "All Lossy Film" not in page
