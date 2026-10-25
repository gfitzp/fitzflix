"""Test the one write path of the track rows, replace_track_rows.

6 sites deleted and inserted the FileAudioTrack and FileSubtitleTrack
rows of a file by hand: the import, the rescan, 3 in-place rewrites
and the Atmos supplement. 5 of the 6 ran the forced-subtitle pass. The
helper runs it on every write."""


def _audio(language, mode="Lossy"):
    return {
        "language": language,
        "language_name": language,
        "format": "AC-3",
        "channels": "6",
        "compression_mode": mode,
    }


def _subtitle(language, elements, forced=False):
    return {
        "language": language,
        "language_name": language,
        "format": "PGS",
        "elements": elements,
        "forced": forced,
    }


def test_replace_track_rows_replaces_numbers_and_flags(app):
    from app import db
    from app.models import FileAudioTrack, FileSubtitleTrack
    from app.tracks import replace_track_rows
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Track Rows", 1972)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.add_all(
            [
                FileAudioTrack(file_id=file.id, track=1, **_audio("deu")),
                FileAudioTrack(file_id=file.id, track=2, **_audio("fra")),
                FileSubtitleTrack(file_id=file.id, track=1, **_subtitle("deu", 5)),
            ]
        )
        db.session.commit()

        audio = [_audio("eng", "Lossless")]
        subtitles = [
            _subtitle("eng", 1000),
            _subtitle("eng", 100),
            _subtitle("eng", 50, forced=True),
        ]
        rows = replace_track_rows(file, audio, subtitles)
        db.session.commit()

        stored_audio = (
            FileAudioTrack.query.filter_by(file_id=file.id)
            .order_by(FileAudioTrack.track)
            .all()
        )
        stored_subtitles = (
            FileSubtitleTrack.query.filter_by(file_id=file.id)
            .order_by(FileSubtitleTrack.track)
            .all()
        )
        assert [row.language for row in stored_audio] == ["eng"]
        assert [row.track for row in stored_audio] == [1]
        assert rows == stored_audio
        assert [row.track for row in stored_subtitles] == [1, 2, 3]
        # The small unforced sibling is unknown now. The forced one keeps
        # its flag. The main track keeps its False.
        assert [row.forced for row in stored_subtitles] == [False, None, True]


def test_no_site_writes_the_track_rows_by_hand():
    import inspect

    import app.atmos as atmos
    import app.importing as importing
    import app.tracks as tracks

    for module in (atmos, importing, tracks):
        source = inspect.getsource(module)
        assert source.count(
            "FileAudioTrack.query.filter_by(file_id=file.id).delete()"
        ) == (1 if module is tracks else 0)
        assert source.count("FileSubtitleTrack(**track)") == (
            1 if module is tracks else 0
        )
