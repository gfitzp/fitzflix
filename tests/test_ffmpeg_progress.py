"""Test the percent of the ffmpeg progress lines of the FLAC supplement (#388)."""


def test_the_flac_progress_stops_at_99(app):
    from app.media.tracks.audio_supplements import ffmpeg_progress

    line = "size=  1024kB time={} bitrate=1000.0kbits/s speed=10x"
    assert ffmpeg_progress(line.format("00:30:00"), 3600, 0) == 50
    assert ffmpeg_progress(line.format("01:00:00"), 3600, 50) == 99
    assert ffmpeg_progress(line.format("01:05:00"), 3600, 99) == 99
    assert ffmpeg_progress("Stream mapping:", 3600, 42) == 42
    assert ffmpeg_progress(line.format("00:30:00"), 0, 7) == 7


def test_the_supplement_task_reports_at_most_99(app, monkeypatch, tmp_path):
    """A time line past the duration reports 99, not 200, in the task itself (#388)."""

    from app.media.tracks import audio_supplements
    from app.media.tracks import edits
    from app.media.tracks import remux
    from app.media.tracks import scan
    import app.media.videos as videos

    source = tmp_path / "long.mkv"
    source.write_bytes(b"source")

    class General:
        track_type = "General"
        format = "Matroska"
        duration = 5000

    class Info:
        tracks = [General()]

        def to_json(self):
            return "{}"

    class FakeProcess:
        stdout = [
            "size=1kB time=00:00:02.00 bitrate=1kbits/s\n",
            "size=2kB time=00:00:10.00 bitrate=1kbits/s\n",
        ]

        def __init__(self, command, **kwargs):
            (tmp_path / ".long.mkv").write_bytes(b"twin")

        def poll(self):
            return 0

        def kill(self):
            pass

    reported = []
    monkeypatch.setattr(
        scan,
        "get_audio_tracks_from_file",
        lambda path: [
            {"streamorder": 1, "format": "MLP FBA", "compression_mode": "Lossless"}
        ],
    )
    monkeypatch.setattr(
        audio_supplements, "plan_audio_supplements", lambda audio: [("flac", 1)]
    )
    monkeypatch.setattr(audio_supplements, "build_supplement_args", lambda plan: [])
    monkeypatch.setattr(scan.MediaInfo, "parse", lambda path: Info())
    monkeypatch.setattr(remux.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(videos, "wait_for_subprocess", lambda process, *a, **k: None)
    monkeypatch.setattr(audio_supplements, "copy_image_attachments", lambda *args: None)
    monkeypatch.setattr(
        remux,
        "report_progress",
        lambda job, name, activity, percent=-1: reported.append(percent),
    )
    monkeypatch.setattr(
        edits,
        "report_progress",
        lambda job, name, activity, percent=-1: reported.append(percent),
    )
    monkeypatch.setattr(
        audio_supplements,
        "report_progress",
        lambda job, name, activity, percent=-1: reported.append(percent),
    )

    with app.app_context():
        assert audio_supplements.supplement_lossless_tracks(str(source)) is True

    assert reported == [40, 99]
