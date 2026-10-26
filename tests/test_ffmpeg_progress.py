"""Test the percent of the ffmpeg progress lines of the FLAC supplement (#388)."""


def test_the_flac_progress_stops_at_99(app):
    from app.tracks import ffmpeg_progress

    line = "size=  1024kB time={} bitrate=1000.0kbits/s speed=10x"
    assert ffmpeg_progress(line.format("00:30:00"), 3600, 0) == 50
    assert ffmpeg_progress(line.format("01:00:00"), 3600, 50) == 99
    assert ffmpeg_progress(line.format("01:05:00"), 3600, 99) == 99
    assert ffmpeg_progress("Stream mapping:", 3600, 42) == 42
    assert ffmpeg_progress(line.format("00:30:00"), 0, 7) == 7
