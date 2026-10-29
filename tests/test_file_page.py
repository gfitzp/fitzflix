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
                    sampling_rate_khz=48,
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

    calls = []
    monkeypatch.setattr(
        library, "enqueue_task", lambda name, *args, **kwargs: calls.append(name)
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


def test_the_scan_form_rescans_the_tracks(app, admin_client, monkeypatch):
    import app.main.library as library

    scanned = []
    monkeypatch.setattr(
        library, "track_metadata_scan", lambda file_id: scanned.append(file_id) or True
    )
    file_id = _file(app)
    page = _post(admin_client, file_id, {"scan_submit": "Scan"})
    assert scanned == [file_id]
    assert "Rescanned track metadata for" in page


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
        sampling_rate_khz=48,
    )
    lossy = FileAudioTrack(
        language_name="French", codec="AC-3", channels="6", bitrate_kbps=640
    )
    bare = FileAudioTrack(language_name="Latin", codec="PCM", channels="2")
    assert lossless.label == "English: TrueHD 8 (24-bit 48 kHz)"
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
