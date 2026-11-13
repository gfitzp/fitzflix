"""Pin the form posts and the track labels of the file page (/file/<id>).

Each form of the page has a handler. The forms that change the local
copy refuse a file that is not present locally."""

import os

import pytest

from app import db
from tests.conftest import ADMIN_PASSWORD, csrf_token_from
from tests.factories import make_movie, make_movie_file

# The track UIDs of the rows of _file. They differ from the positions.
# Thus, a form that posts a position instead of a UID fails (#611).

AUDIO_UIDS = ("9001", "9002")
SUBTITLE_UID = "9003"


def _file(
    app,
    present=True,
    container="Matroska",
    aws_key=None,
    title="Page Film",
    uids=True,
):
    """Make a movie file with 2 audio tracks and 1 subtitle track. Return its id.

    With uids False, the rows have no track UIDs, as before the backfill."""

    from app.models import FileAudioTrack, FileSubtitleTrack

    with app.app_context():
        movie = make_movie(title, 1999)
        file = make_movie_file(movie, "Bluray-1080p", container=container)
        file.aws_key = aws_key
        db.session.add_all(
            [
                FileAudioTrack(
                    file_id=file.id,
                    track=1,
                    uid=AUDIO_UIDS[0] if uids else None,
                    language="eng",
                    language_name="English",
                    codec="TrueHD",
                    channels="8",
                    compression_mode="Lossless",
                    bit_depth=24,
                    sampling_rate=48000,
                    default=True,
                ),
                FileAudioTrack(
                    file_id=file.id,
                    track=2,
                    uid=AUDIO_UIDS[1] if uids else None,
                    language="fra",
                    language_name="French",
                    codec="AC-3",
                    channels="6",
                    compression_mode="Lossy",
                    bitrate_kbps=640,
                ),
                FileSubtitleTrack(
                    file_id=file.id,
                    track=3,
                    uid=SUBTITLE_UID if uids else None,
                    language="eng",
                    language_name="English",
                    elements=1200,
                ),
            ]
        )
        db.session.commit()
        if present:
            path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").close()
        return file.id


@pytest.fixture
def queued(monkeypatch):
    """Record each task that the page queues."""

    import app.main.library as library
    from app import aws_storage

    calls = []
    for module in (library, aws_storage):
        monkeypatch.setattr(
            module, "enqueue_task", lambda name, *args, **kwargs: calls.append(name)
        )
    return calls


def _post(admin_client, file_id, data):
    page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
    return admin_client.post(
        f"/file/{file_id}",
        data={"csrf_token": csrf_token_from(page), **data},
        follow_redirects=True,
    ).get_data(as_text=True)


LOCAL_FORMS = [
    {"scan_submit": "Scan"},
    {"default_audio": "9001", "default_subtitle": "0", "mkvpropedit_submit": "Update"},
    {"audio_tracks": "9001", "subtitle_tracks": "9003", "mkvmerge_submit": "Remux"},
    {"s3_upload_submit": "Upload"},
]


@pytest.mark.parametrize("data", LOCAL_FORMS)
def test_a_local_form_refuses_an_absent_file(app, admin_client, queued, data):
    file_id = _file(app, present=False)
    page = _post(admin_client, file_id, data)
    assert (
        "&#39;Page Film (1999) - [Bluray-1080p].mkv&#39; is not present locally."
        in (page)
    )
    assert queued == []


def test_the_scan_form_queues_one_scan(app, admin_client, monkeypatch):
    """The Scan button queues the scan task. The request reads no file (#695).

    The scan can remux the file. No web request rewrites a library file.
    The job has the id of the lock-busy retry of the scan. Thus, a second
    click while the scan waits queues nothing new."""

    import app.tracks as tracks
    from app.tasks import requeue_job_id

    read = []
    monkeypatch.setattr(
        tracks,
        "extract_track_metadata",
        lambda *args, **kwargs: read.append(args) or {},
    )
    file_id = _file(app)
    wanted = requeue_job_id("app.videos.track_metadata_scan_task", file_id)

    page = _post(admin_client, file_id, {"scan_submit": "Scan"})
    assert "Queued a scan of the track metadata of" in page
    assert app.file_queue.job_ids == [wanted]
    job = app.file_queue.fetch_job(wanted)
    assert job.func_name == "app.videos.track_metadata_scan_task"
    assert job.args == (file_id,)

    page = _post(admin_client, file_id, {"scan_submit": "Scan"})
    assert "A scan of the track metadata of" in page
    assert "is already queued." in page
    assert app.file_queue.job_ids == [wanted]
    assert read == []


def test_the_remux_form_queues_the_remux(app, admin_client, queued):
    file_id = _file(app)
    page = _post(
        admin_client,
        file_id,
        {"audio_tracks": "9001", "subtitle_tracks": "9003", "mkvmerge_submit": "Remux"},
    )
    assert queued == ["app.videos.mkvmerge_task"]
    assert "Remuxing MKV file" in page


def test_the_remux_form_refuses_a_file_that_is_not_matroska(app, admin_client, queued):
    file_id = _file(app, container="MPEG-4")
    page = _post(admin_client, file_id, {"mkvmerge_submit": "Remux"})
    assert queued == []
    assert "because it is not an MKV file!" in page


def test_the_upload_form_queues_the_upload(app, admin_client, queued):
    file_id = _file(app)
    page = _post(admin_client, file_id, {"s3_upload_submit": "Upload"})
    assert queued == ["app.videos.upload_task"]
    assert "to AWS S3 storage" in page


def test_the_restore_form_checks_the_password_and_the_archive(
    app, admin_client, queued
):
    file_id = _file(app, present=False)
    page = _post(
        admin_client,
        file_id,
        {"password": "wrong", "s3_download_submit": "Restore"},
    )
    assert "Incorrect password provided!" in page
    page = _post(
        admin_client,
        file_id,
        {"password": ADMIN_PASSWORD, "s3_download_submit": "Restore"},
    )
    assert "has no archive in AWS to restore." in page
    assert queued == []

    archived_id = _file(
        app, present=False, aws_key="untouched/Archived.mkv", title="Archived Film"
    )
    page = _post(
        admin_client,
        archived_id,
        {"password": ADMIN_PASSWORD, "s3_download_submit": "Restore"},
    )
    assert queued == ["app.videos.aws_restore"]
    assert "to be restored from AWS" in page


def test_the_page_labels_each_track(app, admin_client):
    file_id = _file(app)
    page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
    assert "24-bit 48 kHz" in page
    assert "640 kbps" in page


def test_changed_track_languages(app):
    """A blank box and an unchanged code are no change. A bad value is unresolved."""

    from types import SimpleNamespace

    from app.main.library import changed_track_languages

    audio = [
        SimpleNamespace(track=1, uid="71", language="und"),
        SimpleNamespace(track=2, uid="72", language="ger"),
    ]
    subtitles = [SimpleNamespace(track=3, uid="73", language="eng")]
    with app.app_context():
        changes, unresolved = changed_track_languages(
            {
                "language_71": "eng",
                "language_72": "deu",
                "language_73": "  ",
            },
            audio,
            subtitles,
        )
        assert (changes, unresolved) == ({"71": "eng"}, [])
        changes, unresolved = changed_track_languages(
            {"language_73": "Klingonese"}, audio, subtitles
        )
        assert (changes, unresolved) == ({}, ["Klingonese"])


def test_the_track_labels(app):
    from app.models import FileAudioTrack, FileSubtitleTrack

    lossless = FileAudioTrack(
        language_name="English",
        codec="TrueHD",
        channels="8",
        compression_mode="Lossless",
        bit_depth=24,
        sampling_rate=48000,
    )
    lossy = FileAudioTrack(
        language_name="French", codec="AC-3", channels="6", bitrate_kbps=640
    )
    bare = FileAudioTrack(language_name="Latin", codec="PCM", channels="2")
    assert lossless.label == "English: TrueHD 8 (24-bit 48 kHz)"
    # The rate of a CD is 44.1 kHz. The label must not cut it to 44 (#725).
    cd = FileAudioTrack(
        language_name="English",
        codec="FLAC",
        channels="2",
        compression_mode="Lossless",
        bit_depth=16,
        sampling_rate=44100,
    )
    assert cd.quality_label == "16-bit 44.1 kHz"
    high = FileAudioTrack(
        compression_mode="Lossless", bit_depth=24, sampling_rate=176400
    )
    assert high.quality_label == "24-bit 176.4 kHz"
    assert lossy.label == "French: AC-3 6 (640 kbps)"
    assert bare.label == "Latin: PCM 2" and bare.quality_label is None
    subtitle = FileSubtitleTrack(elements=1200, language_name="English")
    assert subtitle.label == "1200-element English"


def test_the_restore_form_refuses_to_replace_a_newer_local_file(
    app, admin_client, queued
):
    """A stale row with a local file gets a warning, not a restore (#496)."""

    from app.models import File

    file_id = _file(app, aws_key="modified/Newer Page.mkv", title="Newer Page Film")
    with app.app_context():
        file = db.session.get(File, file_id)
        file.aws_stale = True
        db.session.commit()
    page = _post(
        admin_client,
        file_id,
        {"password": ADMIN_PASSWORD, "s3_download_submit": "Restore"},
    )
    assert "is newer than its archive in AWS" in page
    assert queued == []


TRACK_EDIT_POSTS = [
    (
        "app.videos.mkvpropedit_task",
        {"default_audio": "9002", "default_subtitle": "0", "mkvpropedit_submit": "Up"},
        ("9002", "0", []),
    ),
    (
        "app.videos.mkvmerge_task",
        {"audio_tracks": "9002", "subtitle_tracks": "9003", "mkvmerge_submit": "Rx"},
        (["9002"], ["9003"]),
    ),
]


@pytest.mark.parametrize("task, data, expected", TRACK_EDIT_POSTS)
def test_a_track_edit_post_sends_the_track_uids(
    app, admin_client, monkeypatch, task, data, expected
):
    """The page posts the track UIDs, and the task gets them (#611)."""

    import app.main.library as library

    calls = []
    monkeypatch.setattr(
        library,
        "enqueue_task",
        lambda name, *args, **kwargs: calls.append((name, args, kwargs)),
    )
    file_id = _file(app, title=f"UID Post {task[-10:]}")
    page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
    assert 'name="default_audio" value="9002"' in page
    assert 'name="language_9003"' in page
    _post(admin_client, file_id, data)
    ((name, args, kwargs),) = calls
    assert name == task
    assert args[0] == file_id
    assert args[1 : len(expected) + 1] == expected


NO_UID_POSTS = [
    (
        "app.videos.mkvpropedit_task",
        {"default_audio": "None", "default_subtitle": "0", "mkvpropedit_submit": "Up"},
        ("None", "0", []),
    ),
    (
        "app.videos.mkvmerge_task",
        {"audio_tracks": "None", "subtitle_tracks": "None", "mkvmerge_submit": "Rx"},
        (["None"], ["None"]),
    ),
]


@pytest.mark.parametrize("task, data, expected", NO_UID_POSTS)
def test_rows_with_no_uid_post_none_to_the_task(
    app, admin_client, monkeypatch, task, data, expected
):
    """The rows before the backfill have no UIDs. The page posts "None" (#611).

    The file has no track with that UID. Thus, the task refuses the edit
    and queues a rescan (tests/test_track_uids.py)."""

    import app.main.library as library

    calls = []
    monkeypatch.setattr(
        library,
        "enqueue_task",
        lambda name, *args, **kwargs: calls.append((name, args, kwargs)),
    )
    file_id = _file(app, title=f"No UID {task[-10:]}", uids=False)
    page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
    assert 'name="default_audio" value="None" checked aria-label' in page
    _post(admin_client, file_id, data)
    ((name, args, kwargs),) = calls
    assert name == task
    assert args[0] == file_id
    assert args[1 : len(expected) + 1] == expected


STALE_TRACK_POSTS = [
    {"default_audio": "9001", "default_subtitle": "9003", "mkvpropedit_submit": "Up"},
    {"audio_tracks": "9001", "subtitle_tracks": "9003", "mkvmerge_submit": "Rx"},
]

STALE_TRACKS_MESSAGE = "changed after the page was loaded. Nothing was queued."


@pytest.mark.parametrize("data", STALE_TRACK_POSTS)
def test_a_track_post_from_an_old_page_says_that_the_tracks_changed(
    app, admin_client, queued, data
):
    """A track form with a track that is gone gives a message and queues nothing (#763).

    A job removed the subtitle track after the page was loaded. The page
    comes back with the tracks as they are now."""

    from app.models import FileSubtitleTrack

    file_id = _file(app, title=f"Stale Tracks {sorted(data)[-1]}")
    page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
    assert STALE_TRACKS_MESSAGE not in page
    assert 'name="subtitle_tracks" value="9003"' in page
    with app.app_context():
        FileSubtitleTrack.query.filter_by(file_id=file_id).delete()
        db.session.commit()
    response = admin_client.post(
        f"/file/{file_id}", data={"csrf_token": csrf_token_from(page), **data}
    )
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert queued == []
    assert STALE_TRACKS_MESSAGE in page
    assert "The page shows the tracks as they are now." in page
    assert 'name="audio_tracks" value="9001" checked' in page
    assert 'name="subtitle_tracks" value="9003"' not in page
    assert "is not a valid choice" not in page


def test_a_track_post_with_a_bad_csrf_token_stays_quiet(app, admin_client, queued):
    """A csrf error gives no track message, as flash_form_errors does (#763)."""

    file_id = _file(app, title="Quiet Csrf Film")
    page = admin_client.post(
        f"/file/{file_id}",
        data={"csrf_token": "bad", **STALE_TRACK_POSTS[1]},
    ).get_data(as_text=True)
    assert queued == []
    assert STALE_TRACKS_MESSAGE not in page
