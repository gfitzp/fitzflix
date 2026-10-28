"""Test the mkvmerge ids of the track edit (#315).

The form gives the position of a track in its kind. mkvmerge wants the
container stream order. MediaInfo lists the tracks grouped by kind. The
old code took the list index of the first audio track and counted on
from it. A file with a subtitle stream before an audio stream lost the
wrong tracks."""

import pytest

from tests.factories import make_movie, make_movie_file


class FakeTrack:
    """A MediaInfo track with a kind and a stream order."""

    def __init__(self, track_type, streamorder):
        self.track_type = track_type
        self.track_id = streamorder + 1
        self.streamorder = streamorder

    def to_data(self):
        """Return the track fields, as pymediainfo does."""

        return {"track_type": self.track_type, "streamorder": str(self.streamorder)}


class FakeInfo:
    """A MediaInfo result for a file with interleaved streams."""

    # MediaInfo groups by kind. The streams are video 0, subtitle 1,
    # audio 2, audio 3, subtitle 4.

    tracks = [
        FakeTrack("Video", 0),
        FakeTrack("Audio", 2),
        FakeTrack("Audio", 3),
        FakeTrack("Text", 1),
        FakeTrack("Text", 4),
    ]


class Stop(Exception):
    """Stop the task after the command is built."""


def _remux_command(app, monkeypatch, audio, subtitles, info=FakeInfo, refused=False):
    """Run the track edit up to the remux. Return the mkvmerge command.

    With refused, the edit must stop with the "no stream order" error
    before the remux. Otherwise it must reach the remux."""

    import app.tracks as tracks
    from app import db

    with app.app_context():
        movie = make_movie("Interleaved Film", 2001)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id

    commands = []

    def fake_remux(file_path, build, job, basename, label, before_replace=None):
        commands.append(build("in.mkv", "out.mkv"))
        raise Stop()

    monkeypatch.setattr(tracks.MediaInfo, "parse", lambda path: info())
    monkeypatch.setattr(tracks, "remux_file", fake_remux)
    if refused:
        with pytest.raises(RuntimeError, match="no stream order"):
            tracks.mkvmerge_unlocked(file_id, audio, subtitles)
    else:
        with pytest.raises(Stop):
            tracks.mkvmerge_unlocked(file_id, audio, subtitles)
    return commands


def test_the_track_edit_uses_the_stream_order(app, monkeypatch):
    (command,) = _remux_command(app, monkeypatch, ["2"], ["1", "2"])
    pairs = list(zip(command, command[1:]))
    assert ("-a", "3") in pairs
    assert ("-s", "1,4") in pairs


def test_an_unknown_track_stops_the_remux(app, monkeypatch):
    assert _remux_command(app, monkeypatch, ["3"], [], refused=True) == []
