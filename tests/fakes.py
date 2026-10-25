"""The shared stand-ins of the tests: an HTTP answer, a TMDB router, S3, SQS, rq.

Import them inside a test or at the top of a test module. This module
imports no application module. Thus, it is safe at collection time."""

import requests


class Response:
    """A canned HTTP answer for a fake requests.get or tmdb_get.

    json() returns the payload. A status of 400 or more makes
    raise_for_status raise requests.HTTPError with this answer as its
    response. Thus, a caller can read the status of the error."""

    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    @property
    def content(self):
        """Return the body bytes: empty when there is no payload."""

        return b"" if self.payload is None else b"{}"

    def raise_for_status(self):
        """Raise requests.HTTPError for an error status."""

        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.exceptions.HTTPError(
                f"{self.status_code} error", response=response
            )

    def json(self):
        """Return the canned payload."""

        return self.payload


def tmdb_router(routes, default=None, calls=None):
    """Return a fake tmdb_get that answers by the end of the URL.

    routes maps a URL suffix to an answer. The first suffix that ends
    the URL wins. An answer is a payload, a Response, or a function of
    (url, params) that returns either. An unmatched URL answers
    default, or 404 when default is None. calls collects (url, params)
    of each request, if given."""

    def answer(value, url, params):
        if callable(value):
            value = value(url, params)
        return value if isinstance(value, Response) else Response(value)

    def fake_tmdb_get(url, params=None, **kwargs):
        if calls is not None:
            calls.append((url, params))
        for suffix, value in routes.items():
            if url.endswith(suffix):
                return answer(value, url, params)
        if default is None:
            return Response({"status_message": "not found"}, status=404)
        return answer(default, url, params)

    return fake_tmdb_get


class S3:
    """A fake boto3 S3 client for the download paths.

    head is the head_object answer: a dict, an exception to raise, or a
    function of the call number (1, 2, ...) that returns a dict or
    raises. download is the body that download_file writes on success.
    errors is an exception that each download raises, or a list of
    exceptions that the downloads raise in order before a success.
    partial is the body that a failing download writes before it
    raises."""

    def __init__(self, head=None, download=b"s3!", errors=None, partial=None):
        self.head = head if head is not None else {"ContentLength": 3, "ETag": '"v1"'}
        self.body = download
        self.errors = errors
        self.partial = partial
        self.head_calls = 0
        self.attempts = 0
        self.downloads = []

    def head_object(self, Bucket=None, Key=None, **kwargs):
        """Answer the configured head."""

        self.head_calls += 1
        if isinstance(self.head, Exception):
            raise self.head
        if callable(self.head):
            return self.head(self.head_calls)
        return self.head

    def download_file(self, bucket, key, filename, Callback=None):
        """Write the body, or raise the next error."""

        self.attempts += 1
        error = self.errors
        if isinstance(error, list):
            error = error.pop(0) if error else None
        if error is not None:
            if self.partial is not None:
                with open(filename, "wb") as target:
                    target.write(self.partial)
            raise error
        self.downloads.append(key)
        with open(filename, "wb") as target:
            target.write(self.body)


class SQS:
    """A fake SQS client. It records the deleted receipt handles."""

    def __init__(self):
        self.deleted = []

    def delete_message(self, QueueUrl=None, ReceiptHandle=None, **kwargs):
        """Record the receipt handle of the deleted message."""

        self.deleted.append(ReceiptHandle)
        return {}


class Job:
    """A fake rq job. save_meta records a copy of the meta at each save."""

    def __init__(self, meta=None):
        self.meta = dict(meta or {})
        self.saved = []

    def save_meta(self):
        """Record a copy of the meta."""

        self.saved.append(dict(self.meta))


class RecordingQueue:
    """A fake rq queue. enqueue records (args, kwargs) and runs nothing."""

    def __init__(self, name=None):
        self.name = name
        self.calls = []

    def enqueue(self, *args, **kwargs):
        """Record the call."""

        self.calls.append((args, kwargs))
