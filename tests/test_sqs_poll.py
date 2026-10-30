"""Test the hourly SQS poll for completed restores."""

import json

import botocore.exceptions

import app.aws_storage as aws_storage


class FakeSQS:
    """Serve a list of message bodies, 1 at a time. Record the calls."""

    def __init__(self, bodies, fail_visibility=False):
        self.messages = [
            {"Body": json.dumps(body), "ReceiptHandle": f"r{n}"}
            for n, body in enumerate(bodies)
        ]
        self.deleted = []
        self.visibility = []
        self.fail_visibility = fail_visibility

    def receive_message(self, **kwargs):
        self.received_with = kwargs["VisibilityTimeout"]
        if not self.messages:
            return {}
        return {"Messages": [self.messages.pop(0)]}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        if self.fail_visibility:
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "MessageNotInflight"}}, "ChangeMessageVisibility"
            )
        self.visibility.append((ReceiptHandle, VisibilityTimeout))


class FakeS3:
    def restore_object(self, **kwargs):
        pass


def restore_event(key):
    return {
        "Records": [
            {"eventName": "ObjectRestore:Completed", "s3": {"object": {"key": key}}}
        ]
    }


def claim(app, key):
    """Make a File row that claims key. A key with no claim gets no download (#369)."""

    import os

    from app import db
    from tests.factories import make_movie, make_movie_file

    title = os.path.splitext(os.path.basename(key))[0]
    with app.app_context():
        file = make_movie_file(make_movie(title, 2000), "DVD", aws_key=key)
        db.session.commit()
        return file.id


def downloads(app):
    return [
        job.args[0]
        for job in app.file_queue.jobs
        if job.func_name == "app.videos.download_task"
    ]


def test_a_test_event_is_deleted_and_the_restores_behind_it_run(app, monkeypatch):
    """S3 sends an s3:TestEvent with no Records when a notification is written."""

    sqs = FakeSQS(
        [
            {"Service": "Amazon S3", "Event": "s3:TestEvent"},
            restore_event("untouched/Some+Film+%282001%29+-+%5BDVD%5D.mkv"),
        ]
    )
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: FakeS3())
    claim(app, "untouched/Some Film (2001) - [DVD].mkv")

    assert aws_storage.sqs_retrieve_task() is True
    assert sqs.deleted == ["r0"]
    assert sqs.received_with == aws_storage.SQS_VISIBILITY_SECONDS
    with app.app_context():
        assert downloads(app) == ["untouched/Some Film (2001) - [DVD].mkv"]


def test_a_failed_extension_does_not_stop_the_poll(app, monkeypatch):
    """A message that is no longer in flight must not block the new restores."""

    sqs = FakeSQS([restore_event("untouched/New Film (2002) - [DVD].mkv")])
    sqs.fail_visibility = True
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: FakeS3())
    claim(app, "untouched/New Film (2002) - [DVD].mkv")

    with app.app_context():
        app.file_queue.enqueue(
            "app.videos.download_task",
            args=("untouched/Old Film (2000) - [DVD].mkv", "Old Film.mkv", "old"),
            meta={"sqs_receipt_handle": "old"},
        )

    assert aws_storage.sqs_retrieve_task() is True
    with app.app_context():
        assert "untouched/New Film (2002) - [DVD].mkv" in downloads(app)


def test_a_message_that_comes_back_gets_no_second_download(app, monkeypatch):
    """The key already has a download job. The new handle goes under the key (#617)."""

    key = "untouched/Busy Film (2003) - [DVD].mkv"
    sqs = FakeSQS([restore_event(key)])
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: FakeS3())

    with app.app_context():
        job = app.file_queue.enqueue(
            "app.videos.download_task",
            args=(key, "Busy Film (2003) - [DVD].mkv", "old-handle"),
            meta={"sqs_receipt_handle": "old-handle"},
        )

    assert aws_storage.sqs_retrieve_task() is True
    with app.app_context():
        assert downloads(app) == [key]
        assert aws_storage._latest_receipt_handle("old-handle", key) == "r0"
        assert job.id in app.file_queue.job_ids


def test_an_expired_restore_is_requested_again_not_downloaded(app, monkeypatch):
    """S3 keeps the Restore header after the copy expires. That is not readable."""

    key = "untouched/Expired (1999) - [DVD].mkv"
    restored = []

    class ColdS3:
        def list_objects(self, **kwargs):
            return {"Contents": [{"Key": key, "StorageClass": "DEEP_ARCHIVE"}]}

        def head_object(self, **kwargs):
            return {
                "StorageClass": "DEEP_ARCHIVE",
                "Restore": 'ongoing-request="false", '
                'expiry-date="Fri, 01 Jan 2021 00:00:00 GMT"',
            }

        def restore_object(self, **kwargs):
            restored.append(kwargs["Key"])

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: ColdS3())
    aws_storage.aws_restore(key)

    assert restored == [key]
    with app.app_context():
        assert downloads(app) == []


def test_the_poll_renews_a_restore_for_the_full_days(app, monkeypatch):
    """S3 counts the days from now. 1 day cut the 2-day copy short (#320)."""

    requests = []

    class RecordingS3:
        def restore_object(self, **kwargs):
            requests.append(kwargs["RestoreRequest"]["Days"])

    sqs = FakeSQS([])
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: RecordingS3())

    with app.app_context():
        app.file_queue.enqueue(
            "app.videos.download_task",
            args=("untouched/Pending (2003) - [DVD].mkv", "Pending.mkv", "r9"),
            meta={"sqs_receipt_handle": "r9"},
        )

    assert aws_storage.sqs_retrieve_task() is True
    assert requests == [aws_storage.RESTORE_DAYS]
    assert aws_storage.RESTORE_DAYS >= 2


def test_a_new_chain_of_a_key_reads_the_handle_of_its_own_message(app, monkeypatch):
    """A stale record of an older chain never shadows the new message (#587, #617).

    The poll records the handle under the key at each receive. The
    download of the new chain deletes the new message, whatever its job
    id is."""

    key = "untouched/Second Chain (2004) - [DVD].mkv"
    sqs = FakeSQS([restore_event(key)])
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: FakeS3())
    claim(app, key)
    with app.app_context():
        aws_storage.record_receipt_handle(key, "handle-of-the-old-chain")

    assert aws_storage.sqs_retrieve_task() is True
    with app.app_context():
        assert downloads(app) == [key]
        assert aws_storage._latest_receipt_handle(None, key) == "r0"
        assert aws_storage.delete_download_message(None, key, sqs_client=sqs)
    assert sqs.deleted == ["r0"]
    assert app.redis.get(aws_storage.SQS_HANDLE_KEY.format(key=key)) is None
