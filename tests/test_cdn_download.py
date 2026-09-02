"""Tests for the CloudFront download path of aws_download (#224).

With AWS_DOWNLOAD_VIA_CDN set, the bytes of a restore download come
through a CloudFront signed URL, not through the S3 API. The restore
requests, the SQS handling, and the size check stay on the S3 API. These
tests stub the HTTP transport and the URL signing. Thus, they check the
routing of each HTTP status onto the existing download results.
"""

import base64
import os
import urllib.parse

import pytest


class _FakeResponse:
    """A streamed HTTP response with a fixed status and body."""

    def __init__(self, status_code, body=b""):
        self.status_code = status_code
        self._body = body
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_content(self, chunk_size):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]


class _FakeS3:
    """Answer head_object with a fixed response. Record download_file."""

    def __init__(self, head=None):
        self.head = head if head is not None else {"ContentLength": 3}
        self.downloads = []

    def head_object(self, Bucket, Key):
        if isinstance(self.head, Exception):
            raise self.head
        return self.head

    def download_file(self, bucket, key, filename, Callback=None):
        self.downloads.append(key)
        with open(filename, "wb") as f:
            f.write(b"s3!")


class _FakeSQS:
    def __init__(self):
        self.deleted = []

    def delete_message(self, QueueUrl=None, ReceiptHandle=None):
        self.deleted.append(ReceiptHandle)
        return {}


@pytest.fixture
def cdn(app, monkeypatch):
    """Enable the CloudFront path with a stubbed signer and transport.

    Return a record of the signed keys and the queued responses. Each
    download attempt pops the next response."""

    from app import aws_storage

    monkeypatch.setitem(app.config, "AWS_DOWNLOAD_VIA_CDN", True)
    monkeypatch.setitem(app.config, "CDN_DOMAIN", "d1.cloudfront.net")
    monkeypatch.setitem(app.config, "CDN_KEY_PAIR_ID", "KTEST")
    monkeypatch.setitem(app.config, "CDN_PRIVATE_KEY", "/nonexistent.pem")
    monkeypatch.setattr(aws_storage, "DOWNLOAD_RETRY_SLEEP", lambda seconds: None)

    record = {"signed": [], "responses": [], "requested": []}

    def fake_sign(key, expires_in=None):
        record["signed"].append(key)
        return f"https://d1.cloudfront.net/{key}?Signature=n{len(record['signed'])}"

    def fake_get(url, stream=True, timeout=None):
        record["requested"].append(url)
        return record["responses"].pop(0)

    monkeypatch.setattr(aws_storage, "cdn_signed_url", fake_sign)
    monkeypatch.setattr(aws_storage, "CDN_HTTP_GET", fake_get)
    return record


def _wire(monkeypatch, s3, sqs):
    from app import aws_storage

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)


def test_flag_off_keeps_the_s3_transport(app, monkeypatch):
    """Test that the S3 path is unchanged when the flag is off.

    Without AWS_DOWNLOAD_VIA_CDN, download_file runs and no HTTP request
    goes out, even when the CDN settings are present."""

    from app import aws_storage

    monkeypatch.setitem(app.config, "CDN_DOMAIN", "d1.cloudfront.net")
    requested = []
    monkeypatch.setattr(
        aws_storage, "CDN_HTTP_GET", lambda *a, **k: requested.append(a)
    )
    s3 = _FakeS3()
    _wire(monkeypatch, s3, _FakeSQS())

    with app.app_context():
        assert (
            aws_storage.aws_download("untouched/x.mkv", "x.mkv")
            == aws_storage.DOWNLOAD_COMPLETE
        )
        assert os.path.exists(os.path.join(app.config["IMPORT_DIR"], "x.mkv"))

    assert s3.downloads == ["untouched/x.mkv"]
    assert requested == []


def test_flag_on_without_settings_gives_up(app, monkeypatch):
    """Test that an incomplete CDN configuration stops the download.

    A silent fallback to S3 egress would cost money. The function returns
    False without a transfer, and the SQS message stays for a later
    delivery."""

    from app import aws_storage

    monkeypatch.setitem(app.config, "AWS_DOWNLOAD_VIA_CDN", True)
    monkeypatch.setitem(app.config, "CDN_DOMAIN", "d1.cloudfront.net")
    s3 = _FakeS3()
    sqs = _FakeSQS()
    _wire(monkeypatch, s3, sqs)

    with app.app_context():
        assert aws_storage.aws_download("untouched/x.mkv", "x.mkv", "r1") is False

    assert s3.downloads == []
    assert sqs.deleted == []


def test_cdn_download_lands_the_file(app, monkeypatch, cdn):
    """Test that a 200 through CloudFront completes the download.

    The bytes land in the import directory under the final name, the
    SQS message goes away, and download_file never runs."""

    from app import aws_storage

    cdn["responses"] = [_FakeResponse(200, b"cdn")]
    s3 = _FakeS3()
    sqs = _FakeSQS()
    _wire(monkeypatch, s3, sqs)

    with app.app_context():
        assert (
            aws_storage.aws_download("untouched/x.mkv", "x.mkv", "r1")
            == aws_storage.DOWNLOAD_COMPLETE
        )
        final = os.path.join(app.config["IMPORT_DIR"], "x.mkv")
        assert open(final, "rb").read() == b"cdn"
        assert not os.path.exists(os.path.join(app.config["IMPORT_DIR"], ".x.mkv"))

    assert s3.downloads == []
    assert sqs.deleted == ["r1"]
    assert cdn["signed"] == ["untouched/x.mkv"]


def test_cdn_retry_signs_a_new_url(app, monkeypatch, cdn):
    """Test that each attempt signs a new URL.

    A 503 spends one retry. The second attempt must not reuse the first
    URL, because a short expiry can pass during the backoff."""

    from app import aws_storage

    cdn["responses"] = [_FakeResponse(503), _FakeResponse(200, b"cdn")]
    _wire(monkeypatch, _FakeS3(), _FakeSQS())

    with app.app_context():
        assert (
            aws_storage.aws_download("untouched/x.mkv", "x.mkv")
            == aws_storage.DOWNLOAD_COMPLETE
        )

    assert len(cdn["signed"]) == 2
    assert cdn["requested"][0] != cdn["requested"][1]


def test_cdn_short_body_is_retried(app, monkeypatch, cdn):
    """Test that a body shorter than Content-Length spends a retry."""

    from app import aws_storage

    short = _FakeResponse(200, b"cd")
    short.headers["Content-Length"] = "3"
    cdn["responses"] = [short, _FakeResponse(200, b"cdn")]
    _wire(monkeypatch, _FakeS3(), _FakeSQS())

    with app.app_context():
        assert (
            aws_storage.aws_download("untouched/x.mkv", "x.mkv")
            == aws_storage.DOWNLOAD_COMPLETE
        )
        final = os.path.join(app.config["IMPORT_DIR"], "x.mkv")
        assert open(final, "rb").read() == b"cdn"

    assert len(cdn["signed"]) == 2


def test_cdn_short_body_without_content_length_is_retried(app, monkeypatch, cdn):
    """Test that the S3 object size catches a short body.

    Without a Content-Length header, the body length is checked against
    the size that the S3 HEAD reported. A short body spends a retry and
    never becomes a visible import."""

    from app import aws_storage

    short = _FakeResponse(200, b"cd")
    del short.headers["Content-Length"]
    cdn["responses"] = [short, _FakeResponse(200, b"cdn")]
    _wire(monkeypatch, _FakeS3(head={"ContentLength": 3}), _FakeSQS())

    with app.app_context():
        assert (
            aws_storage.aws_download("untouched/x.mkv", "x.mkv")
            == aws_storage.DOWNLOAD_COMPLETE
        )
        final = os.path.join(app.config["IMPORT_DIR"], "x.mkv")
        assert open(final, "rb").read() == b"cdn"

    assert len(cdn["signed"]) == 2


def test_cdn_404_is_a_missing_object(app, monkeypatch, cdn):
    """Test that a 404 through CloudFront reports a missing object.

    There is no retry. The SQS message goes away, and no partial file
    remains."""

    from app import aws_storage

    cdn["responses"] = [_FakeResponse(404)]
    sqs = _FakeSQS()
    _wire(monkeypatch, _FakeS3(), sqs)

    with app.app_context():
        assert (
            aws_storage.aws_download("untouched/x.mkv", "x.mkv", "r404")
            == aws_storage.DOWNLOAD_OBJECT_MISSING
        )
        assert not os.path.exists(os.path.join(app.config["IMPORT_DIR"], ".x.mkv"))

    assert sqs.deleted == ["r404"]
    assert len(cdn["signed"]) == 1


def test_cdn_403_on_a_cold_object_requests_a_restore(app, monkeypatch, cdn):
    """Test that a 403 on an object back in cold storage defers.

    CloudFront answers 403 for an archived object. The S3 HEAD shows no
    Restore header and a cold storage class. Thus, the function requests
    a new restore and drops the stale SQS message."""

    from app import aws_storage

    cdn["responses"] = [_FakeResponse(403)]
    s3 = _FakeS3(head={"ContentLength": 3, "StorageClass": "DEEP_ARCHIVE"})
    sqs = _FakeSQS()
    restored = []
    _wire(monkeypatch, s3, sqs)
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key: restored.append(key))

    with app.app_context():
        assert (
            aws_storage.aws_download("untouched/x.mkv", "x.mkv", "rstale")
            == aws_storage.DOWNLOAD_RESTORE_PENDING
        )

    assert restored == ["untouched/x.mkv"]
    assert sqs.deleted == ["rstale"]


def test_cdn_403_during_a_restore_waits(app, monkeypatch, cdn):
    """Test that a 403 during a restore in progress waits.

    The HEAD shows an ongoing restore. The function requests no new
    restore. It drops the stale message and waits for the notification."""

    from app import aws_storage

    cdn["responses"] = [_FakeResponse(403)]
    s3 = _FakeS3(
        head={
            "ContentLength": 3,
            "StorageClass": "DEEP_ARCHIVE",
            "Restore": 'ongoing-request="true"',
        }
    )
    sqs = _FakeSQS()
    restored = []
    _wire(monkeypatch, s3, sqs)
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key: restored.append(key))

    with app.app_context():
        assert (
            aws_storage.aws_download("untouched/x.mkv", "x.mkv", "rwait")
            == aws_storage.DOWNLOAD_RESTORE_PENDING
        )

    assert restored == []
    assert sqs.deleted == ["rwait"]


def test_cdn_403_on_an_expired_restore_requests_a_restore(app, monkeypatch, cdn):
    """Test that a past expiry-date counts as an expired restore.

    S3 can keep the Restore header for a while after the restored copy
    is gone. A 403 with such a header must not count as an access fault.
    The function requests a new restore and drops the stale message.
    Without this, the message would come back each visibility timeout
    forever."""

    from app import aws_storage

    cdn["responses"] = [_FakeResponse(403)]
    s3 = _FakeS3(
        head={
            "ContentLength": 3,
            "StorageClass": "DEEP_ARCHIVE",
            "Restore": 'ongoing-request="false", expiry-date="Mon, 01 Sep 2025 00:00:00 GMT"',
        }
    )
    sqs = _FakeSQS()
    restored = []
    _wire(monkeypatch, s3, sqs)
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key: restored.append(key))

    with app.app_context():
        assert (
            aws_storage.aws_download("untouched/x.mkv", "x.mkv", "rexpired")
            == aws_storage.DOWNLOAD_RESTORE_PENDING
        )

    assert restored == ["untouched/x.mkv"]
    assert sqs.deleted == ["rexpired"]


def test_cdn_403_on_a_readable_object_gives_up(app, monkeypatch, cdn):
    """Test that a 403 on a readable object is an access fault.

    The HEAD shows a restored copy. Thus, the 403 comes from the
    signature or from the WAF allowlist, and a retry cannot clear it.
    The function gives up, keeps the SQS message, and removes the
    partial file."""

    from app import aws_storage

    cdn["responses"] = [_FakeResponse(403)]
    s3 = _FakeS3(
        head={
            "ContentLength": 3,
            "StorageClass": "DEEP_ARCHIVE",
            "Restore": 'ongoing-request="false", expiry-date="Fri, 01 Jan 2100 00:00:00 GMT"',
        }
    )
    sqs = _FakeSQS()
    restored = []
    _wire(monkeypatch, s3, sqs)
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key: restored.append(key))

    with app.app_context():
        assert aws_storage.aws_download("untouched/x.mkv", "x.mkv", "r403") is False
        assert not os.path.exists(os.path.join(app.config["IMPORT_DIR"], ".x.mkv"))

    assert restored == []
    assert sqs.deleted == []
    assert len(cdn["signed"]) == 1


def test_cdn_400_gives_up_without_retry(app, monkeypatch, cdn):
    """Test that a client error other than 403 and 404 is terminal."""

    from app import aws_storage

    cdn["responses"] = [_FakeResponse(400)]
    _wire(monkeypatch, _FakeS3(), _FakeSQS())

    with app.app_context():
        assert aws_storage.aws_download("untouched/x.mkv", "x.mkv") is False

    assert len(cdn["signed"]) == 1


def test_cdn_transport_error_spends_a_retry(app, monkeypatch, cdn, caplog):
    """Test that a requests transport error spends one retry.

    A connect-phase error from requests names the full signed URL in its
    message. The traceback reaches the log, but the Signature parameter
    must not."""

    import logging

    import requests

    from urllib3.exceptions import MaxRetryError

    from app import aws_storage
    from app.redaction import REDACTED

    calls = []

    def flaky_get(url, stream=True, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError(
                MaxRetryError(None, url, "NameResolutionError")
            )
        return _FakeResponse(200, b"cdn")

    monkeypatch.setattr(aws_storage, "CDN_HTTP_GET", flaky_get)
    _wire(monkeypatch, _FakeS3(), _FakeSQS())

    with app.app_context():
        with caplog.at_level(logging.ERROR):
            assert (
                aws_storage.aws_download("untouched/x.mkv", "x.mkv")
                == aws_storage.DOWNLOAD_COMPLETE
            )

    assert len(calls) == 2
    assert "Max retries exceeded" in caplog.text
    assert "Signature=n1" not in caplog.text
    assert f"Signature={REDACTED}" in caplog.text


def test_object_is_readable():
    """Test the readable check for each restore state."""

    from app.aws_storage import _object_is_readable

    assert _object_is_readable({})
    assert _object_is_readable({"StorageClass": "STANDARD"})
    assert not _object_is_readable({"StorageClass": "DEEP_ARCHIVE"})
    assert not _object_is_readable({"StorageClass": "GLACIER"})
    assert not _object_is_readable(
        {"StorageClass": "DEEP_ARCHIVE", "Restore": 'ongoing-request="true"'}
    )
    assert _object_is_readable(
        {
            "StorageClass": "DEEP_ARCHIVE",
            "Restore": 'ongoing-request="false", expiry-date="Fri, 01 Jan 2100 00:00:00 GMT"',
        }
    )
    assert not _object_is_readable(
        {
            "StorageClass": "DEEP_ARCHIVE",
            "Restore": 'ongoing-request="false", expiry-date="Mon, 01 Sep 2025 00:00:00 GMT"',
        }
    )

    # An unreadable expiry-date does not turn a restored copy into an
    # expired one

    assert _object_is_readable(
        {
            "StorageClass": "DEEP_ARCHIVE",
            "Restore": 'ongoing-request="false", expiry-date="x"',
        }
    )


def test_cdn_signed_url_verifies_with_the_public_key(app, monkeypatch, tmp_path):
    """Test the signed URL against the CloudFront canned policy.

    The key is percent-encoded in the path. The query holds Expires,
    Signature, and Key-Pair-Id. The signature, after the CloudFront
    substitutions are undone, verifies against the canned policy with
    the public half of the key."""

    from datetime import datetime, timezone

    from botocore.signers import CloudFrontSigner
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from app import aws_storage

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "cf.pem"
    pem.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setitem(app.config, "CDN_DOMAIN", "d1.cloudfront.net")
    monkeypatch.setitem(app.config, "CDN_KEY_PAIR_ID", "KTEST")
    monkeypatch.setitem(app.config, "CDN_PRIVATE_KEY", str(pem))
    monkeypatch.setitem(app.config, "CDN_URL_EXPIRY", 60)

    with app.app_context():
        url = aws_storage.cdn_signed_url("untouched/Thing (2021) - [DVD].mkv")

    parts = urllib.parse.urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == "d1.cloudfront.net"
    assert parts.path == "/untouched/Thing%20%282021%29%20-%20%5BDVD%5D.mkv"
    query = urllib.parse.parse_qs(parts.query)
    assert query["Key-Pair-Id"] == ["KTEST"]
    expires = int(query["Expires"][0])
    assert 0 < expires - int(datetime.now(timezone.utc).timestamp()) <= 60

    base_url = urllib.parse.urlunsplit(parts._replace(query=""))
    policy = CloudFrontSigner("KTEST", lambda m: b"").build_policy(
        base_url, datetime.fromtimestamp(expires, timezone.utc)
    )
    encoded = (
        query["Signature"][0].replace("-", "+").replace("_", "=").replace("~", "/")
    )
    signature = base64.b64decode(encoded)
    private_key.public_key().verify(
        signature, policy.encode("utf8"), padding.PKCS1v15(), hashes.SHA1()
    )
