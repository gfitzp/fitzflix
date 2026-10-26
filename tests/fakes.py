"""The shared stand-ins of the tests: an HTTP answer, a TMDB router, S3, SQS, rq.

ArchiveS3 holds archive objects for the archive moves (#369). Sonarr
holds the series list of the Sonarr path check (#370).

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


class Sonarr:
    """A fake _sonarr for the series calls of the Sonarr path check (#370).

    series is a list of Sonarr series dicts. A GET of /api/v3/series
    lists them, or the ones with the tvdbId of the query. A PUT replaces
    a series and is recorded in puts. A command is recorded in commands.
    down makes each call raise ConnectionError."""

    def __init__(self, series=(), down=False):
        self.series = {entry["id"]: dict(entry) for entry in series}
        self.puts = []
        self.commands = []
        self.gets = []
        self.down = down

    def __call__(self, method, path, payload=None):
        """Answer one Sonarr API call."""

        if self.down:
            raise ConnectionError("Sonarr is down")
        if method == "GET" and path.startswith("/api/v3/series"):
            self.gets.append(path)
            entries = [dict(entry) for entry in self.series.values()]
            if "?tvdbId=" in path:
                tvdb_id = int(path.split("?tvdbId=")[1])
                entries = [e for e in entries if e.get("tvdbId") == tvdb_id]
            return entries
        if method == "PUT" and path.startswith("/api/v3/series/"):
            self.puts.append(dict(payload))
            self.series[payload["id"]] = dict(payload)
            return payload
        if method == "POST" and path == "/api/v3/command":
            self.commands.append(payload)
            return {"id": len(self.commands)}
        raise AssertionError(f"unexpected Sonarr call {method} {path}")


class ArchiveS3:
    """A fake boto3 S3 client that holds the archive objects in a dict.

    objects maps a key to its head: ContentLength, ETag, StorageClass,
    Restore and VersionId. copy puts a STANDARD copy at the new key with
    a new version id. copy_etag is the ETag of each copy, for a copy
    that is not equal. A head with PartNumber answers part_size. events
    records each call in order. on_copy runs at each copy, and on_delete
    at each delete, with the key."""

    def __init__(
        self,
        objects=None,
        part_size=8388608,
        copy_etag=None,
        on_copy=None,
        on_delete=None,
    ):
        self.objects = {key: dict(head) for key, head in (objects or {}).items()}
        self.part_size = part_size
        self.copy_etag = copy_etag
        self.on_copy = on_copy
        self.on_delete = on_delete
        self.events = []
        self.copies = []
        self.configs = []
        self.deletes = []
        self.restores = []
        self.versions = 0

    def head_object(self, Bucket=None, Key=None, PartNumber=None, **kwargs):
        """Answer the head of the key, or raise a 404 ClientError."""

        import botocore.exceptions

        self.events.append(("head", Key))
        if Key not in self.objects:
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        head = dict(self.objects[Key])
        if PartNumber:
            head["ContentLength"] = min(self.part_size, head["ContentLength"])
        return head

    def copy(self, CopySource, Bucket, Key, ExtraArgs=None, Config=None, **kwargs):
        """Copy the source object to Key as a new STANDARD version."""

        source = self.objects[CopySource["Key"]]
        self.versions += 1
        self.events.append(("copy", CopySource["Key"], Key))
        self.copies.append((CopySource["Key"], Key))
        self.configs.append(Config)
        if self.on_copy:
            self.on_copy(CopySource["Key"], Key)
        self.objects[Key] = {
            "ContentLength": source["ContentLength"],
            "ETag": self.copy_etag or source["ETag"],
            "StorageClass": "STANDARD",
            "VersionId": f"copy-{self.versions}",
        }

    def delete_object(self, Bucket=None, Key=None, VersionId=None, **kwargs):
        """Remove the key. Record the key and the version id."""

        self.events.append(("delete", Key, VersionId))
        if self.on_delete:
            self.on_delete(Key)
        self.deletes.append((Key, VersionId))
        self.objects.pop(Key, None)
        return {}

    def list_objects(self, Bucket=None, Prefix="", MaxKeys=1000, **kwargs):
        """List the keys that start with Prefix."""

        keys = sorted(key for key in self.objects if key.startswith(Prefix))[:MaxKeys]
        if not keys:
            return {}
        return {
            "Contents": [
                {
                    "Key": key,
                    "StorageClass": self.objects[key].get("StorageClass", "STANDARD"),
                }
                for key in keys
            ]
        }

    def restore_object(self, Bucket=None, Key=None, RestoreRequest=None, **kwargs):
        """Record the restore request and its tier."""

        self.events.append(("restore", Key))
        self.restores.append((Key, RestoreRequest["GlacierJobParameters"]["Tier"]))
        if Key in self.objects:
            self.objects[Key]["Restore"] = 'ongoing-request="true"'
        return {}
