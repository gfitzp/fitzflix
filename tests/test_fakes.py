"""Test that the shared fakes fail as the real tools do (#446).

A fake that is more lenient than the real tool lets a test pass on
behaviour that production does not have. Each test here runs 1 failure
mode of a fake. The tests that run application code with a tightened
fake are in the test files of that code."""

import json

import botocore.exceptions
import pytest
import requests

from tests.fakes import SQS, ArchiveS3, Sonarr


def _code(error):
    return error.value.response["Error"]["Code"]


def test_a_plain_delete_of_a_versioned_key_leaves_a_marker():
    """A delete without a VersionId hides the object. The version stays."""

    fake = ArchiveS3(
        {"untouched/a.mkv": {"ContentLength": 1, "ETag": '"a"', "VersionId": "v1"}}
    )
    fake.delete_object(Key="untouched/a.mkv")

    assert "untouched/a.mkv" not in fake.objects
    assert [head["VersionId"] for head in fake.noncurrent["untouched/a.mkv"]] == ["v1"]
    listing = fake.list_object_versions(Prefix="untouched/a.mkv")
    assert len(listing["DeleteMarkers"]) == 1
    assert [entry["VersionId"] for entry in listing["Versions"]] == ["v1"]


def test_a_version_delete_makes_the_older_version_current_again():
    fake = ArchiveS3(
        {"untouched/b.mkv": {"ContentLength": 1, "ETag": '"old"', "VersionId": "v1"}}
    )
    fake.copy({"Key": "untouched/b.mkv"}, "bucket", "untouched/b.mkv")
    new_version = fake.objects["untouched/b.mkv"]["VersionId"]

    fake.delete_object(Key="untouched/b.mkv", VersionId=new_version)

    assert fake.objects["untouched/b.mkv"]["VersionId"] == "v1"
    assert fake.noncurrent["untouched/b.mkv"] == []


def test_a_copy_of_a_missing_key_raises_no_such_key():
    fake = ArchiveS3()
    with pytest.raises(botocore.exceptions.ClientError) as error:
        fake.copy({"Key": "untouched/none.mkv"}, "bucket", "untouched/new.mkv")
    assert _code(error) == "NoSuchKey"


def test_a_copy_of_deep_archive_needs_a_finished_restore():
    cold = {"ContentLength": 1, "ETag": '"c"', "StorageClass": "DEEP_ARCHIVE"}
    fake = ArchiveS3({"untouched/cold.mkv": dict(cold)})
    with pytest.raises(botocore.exceptions.ClientError) as error:
        fake.copy({"Key": "untouched/cold.mkv"}, "bucket", "untouched/warm.mkv")
    assert _code(error) == "InvalidObjectState"

    fake.objects["untouched/cold.mkv"][
        "Restore"
    ] = 'ongoing-request="false", expiry-date="Fri, 30 Oct 2026 00:00:00 GMT"'
    fake.copy({"Key": "untouched/cold.mkv"}, "bucket", "untouched/warm.mkv")
    assert fake.objects["untouched/warm.mkv"]["StorageClass"] == "STANDARD"


def test_an_sqs_message_comes_back_after_its_visibility_ends():
    clock = [0.0]
    sqs = SQS([json.dumps({"Records": []})], clock=lambda: clock[0])

    first = sqs.receive_message(VisibilityTimeout=10)["Messages"][0]
    assert sqs.receive_message(VisibilityTimeout=10) == {}
    clock[0] = 11
    second = sqs.receive_message(VisibilityTimeout=10)["Messages"][0]

    assert second["Body"] == first["Body"]
    assert second["ReceiptHandle"] != first["ReceiptHandle"]


def test_a_stale_sqs_handle_cannot_extend_or_delete_the_message():
    clock = [0.0]
    sqs = SQS(["body"], clock=lambda: clock[0])
    stale = sqs.receive_message(VisibilityTimeout=10)["Messages"][0]["ReceiptHandle"]
    clock[0] = 11
    current = sqs.receive_message(VisibilityTimeout=10)["Messages"][0]["ReceiptHandle"]

    with pytest.raises(botocore.exceptions.ClientError) as error:
        sqs.change_message_visibility(ReceiptHandle=stale, VisibilityTimeout=60)
    assert _code(error) == "MessageNotInflight"
    sqs.delete_message(ReceiptHandle=stale)
    assert len(sqs.messages) == 1

    sqs.change_message_visibility(ReceiptHandle=current, VisibilityTimeout=60)
    sqs.delete_message(ReceiptHandle=current)
    assert sqs.messages == []


def test_sonarr_refuses_an_unknown_series_id():
    fake = Sonarr([{"id": 5, "tvdbId": 1, "path": "/tv/Show"}])
    for call in (
        lambda: fake("PUT", "/api/v3/series/9", {"id": 9, "path": "/tv/Other"}),
        lambda: fake("GET", "/api/v3/series/9"),
    ):
        with pytest.raises(requests.exceptions.HTTPError) as error:
            call()
        assert error.value.response.status_code == 404
    assert fake.puts == []
    assert fake("GET", "/api/v3/series/5")["path"] == "/tv/Show"
    with pytest.raises(AssertionError):
        fake("GET", "/api/v3/series?title=Show")


def test_a_poll_with_a_stale_handle_records_the_new_one(app, monkeypatch):
    """The SQS fake redelivers a message to a waiting download (#446).

    The extension with the old handle fails as in SQS. The poll goes on,
    and the job gets the new handle of the message."""

    import app.aws_storage as aws_storage
    from app.aws_storage import SQS_HANDLE_KEY

    key = "untouched/Stale Handle (1999) - [DVD].mkv"
    body = json.dumps(
        {
            "Records": [
                {"eventName": "ObjectRestore:Completed", "s3": {"object": {"key": key}}}
            ]
        }
    )
    clock = [0.0]
    sqs = SQS([body], clock=lambda: clock[0])
    stale = sqs.receive_message(VisibilityTimeout=10)["Messages"][0]["ReceiptHandle"]
    clock[0] = 11

    class FakeS3:
        def restore_object(self, **kwargs):
            pass

    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: FakeS3())
    job = app.file_queue.enqueue(
        "app.videos.download_task",
        key,
        "Stale Handle (1999) - [DVD].mkv",
        stale,
        meta={"sqs_receipt_handle": stale},
    )

    with app.app_context():
        assert aws_storage.sqs_retrieve_task() is True

    assert sqs.visibility == []
    recorded = app.redis.get(SQS_HANDLE_KEY.format(job_id=job.id)).decode()
    assert recorded not in (None, stale)
    assert sqs.messages[0]["handle"] == recorded


def test_a_series_that_left_sonarr_before_the_put_is_not_repointed(app, monkeypatch):
    """Sonarr deletes the series between the GET and the PUT. The PUT 404s (#446).

    _update_sonarr_path raises, and the path check of the caller logs it
    and goes on."""

    import app.sonarr_push as sonarr_push
    from app import db
    from app.series_rename import _update_sonarr_path
    from tests.factories import make_tv_series

    fake = Sonarr(
        [{"id": 5, "tvdbId": 424242, "path": "/tv/Gone Show", "title": "Gone Show"}],
        after_get=lambda sonarr: sonarr.series.clear(),
    )
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    monkeypatch.setattr(sonarr_push, "sonarr_configured", lambda: True)
    with app.app_context():
        series = make_tv_series("Gone Show", tvdb_id=424242)
        db.session.commit()
        with pytest.raises(requests.exceptions.HTTPError):
            _update_sonarr_path(series, "Gone Show", "Gone Show (2001)")
    assert fake.puts == [] and fake.commands == []
