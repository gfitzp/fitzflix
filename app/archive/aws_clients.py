"""The S3 and SQS clients, and the small functions that all the archive code uses."""

import email.utils
import hashlib
import os
import re
import threading
from datetime import datetime, timezone
import boto3
import botocore
import rq
from botocore.client import Config
from rq import get_current_job
from s3transfer.utils import ChunksizeAdjuster
from flask import current_app
from werkzeug.local import LocalProxy
from app.tasks import report_progress
from app import get_app
from app.media.importing.filenames import sanitize_string

app = LocalProxy(get_app)


EIGHT_MEGABYTES = 8388608


def aws_s3_client(with_retries=False):
    """Build an S3 client using the application credentials."""

    kwargs = {
        "aws_access_key_id": current_app.config["AWS_ACCESS_KEY"],
        "aws_secret_access_key": current_app.config["AWS_SECRET_KEY"],
    }
    if with_retries:
        kwargs["config"] = Config(
            connect_timeout=20, retries={"mode": "standard", "max_attempts": 10}
        )
    return boto3.client("s3", **kwargs)


def aws_sqs_client():
    """Build an SQS client using the application credentials."""

    return boto3.client(
        "sqs",
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
        region_name="us-east-1",
    )


class UploadProgressPercentage(object):
    """Report the upload progress as a callback during an upload to AWS S3."""

    def __init__(self, file_path):
        self._file_path = file_path
        self._size = float(os.path.getsize(file_path))
        self._seen_so_far = 0
        self._previous_percent = None
        self._lock = threading.Lock()
        self._job = rq.get_current_job()

    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount

            # Report a zero-byte file as complete. Do not divide by zero.

            percent = int((self._seen_so_far / self._size) * 100) if self._size else 100

            # The transfer callback runs much more frequently than the tool
            # writes an output line. Thus, the log line and the job-meta
            # write occur only when the percentage changes.

            if percent == self._previous_percent:
                return
            self._previous_percent = percent

            app.logger.info(
                f"'{os.path.basename(self._file_path)}' Uploading to AWS: {percent}%"
            )
            report_progress(
                self._job,
                os.path.basename(self._file_path),
                "Uploading to AWS",
                percent,
            )


class DownloadProgressPercentage(object):
    """Report the download progress as a callback during a download from S3."""

    def __init__(self, client, bucket, key, basename):
        self._file_path = basename
        head = client.head_object(Bucket=bucket, Key=key)
        self.size = head.get("ContentLength", 0)
        self.etag = head.get("ETag")
        app.logger.info(f"'{basename}' Download size: {self.size} bytes")
        self._seen_so_far = 0
        self._previous_percent = None
        self._lock = threading.Lock()
        self._job = rq.get_current_job()

    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount

            # Report a zero-byte object as complete. Do not divide by zero.

            percent = int((self._seen_so_far / self.size) * 100) if self.size else 100

            # The transfer callback runs much more frequently than the tool
            # writes an output line. Thus, the log line and the job-meta
            # write occur only when the percentage changes.

            if percent == self._previous_percent:
                return
            self._previous_percent = percent

            app.logger.info(
                f"'{os.path.basename(self._file_path)}' Downloading from AWS: {percent}%"
            )
            report_progress(
                self._job,
                os.path.basename(self._file_path),
                "Downloading from AWS",
                percent,
            )


def _is_missing(error):
    """Return True if a botocore ClientError says that the object does not exist."""

    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


# An object in one of these storage classes needs a restore before a GET
# can read it.

COLD_STORAGE_CLASSES = ("GLACIER", "DEEP_ARCHIVE")


def _object_is_readable(head_response):
    """Return True if a GET can read the object now.

    That is the case for an object in a warm storage class, and for a
    cold object with a restored copy that has not expired. S3 can keep
    the Restore header for a while after the copy is gone. Thus, a past
    expiry-date counts as expired."""

    restore_status = head_response.get("Restore") or ""
    if 'ongoing-request="false"' in restore_status:
        return not _restore_has_expired(restore_status)
    if 'ongoing-request="true"' in restore_status:
        return False
    storage_class = head_response.get("StorageClass") or "STANDARD"
    return storage_class not in COLD_STORAGE_CLASSES


def _restore_has_expired(restore_status):
    """Return True if the expiry-date in a Restore header is in the past.

    A header without a readable expiry-date counts as not expired."""

    match = re.search(r'expiry-date="([^"]+)"', restore_status)
    if not match:
        return False
    try:
        expiry = email.utils.parsedate_to_datetime(match.group(1))
    except (TypeError, ValueError):
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= datetime.now(timezone.utc)


def _head_or_none(s3_client, key):
    """Return the head of key, or None when the object does not exist."""

    try:
        return s3_client.head_object(Bucket=current_app.config["AWS_BUCKET"], Key=key)
    except botocore.exceptions.ClientError as error:
        if _is_missing(error):
            return None
        raise


def calculate_etag(file_path):
    """Calculate the unique ETag for a local file."""

    basename = os.path.basename(file_path)
    current_app.logger.info(f"'{basename}' Calculating ETag")
    job = get_current_job()

    file_size = os.path.getsize(file_path)
    if file_size < EIGHT_MEGABYTES:
        # The file is smaller than 8 MB. Read it in 1 step. Return its MD5
        # hash.

        with open(file_path, "rb") as f:
            md5_hash = hashlib.md5(f.read())

        return md5_hash.hexdigest()

    else:
        md5_digests = []

        # Read the file in the part size of the upload. That is 8 MB, but
        # s3transfer doubles it for a file of more than 10,000 parts (78
        # GiB). With 8 MB parts, the ETag of such a file never matched, and
        # each check uploaded the whole file again.

        part_size = ChunksizeAdjuster().adjust_chunksize(EIGHT_MEGABYTES, file_size)
        with open(file_path, "rb") as f:
            previous_percent = None
            for chunk in iter(lambda: f.read(part_size), b""):
                # Collect the MD5 hashes.
                md5_digests.append(hashlib.md5(chunk).digest())
                percent = int((f.tell() / file_size) * 100)
                if previous_percent != percent:
                    current_app.logger.info(
                        f"'{basename}' Calculating ETag: {percent}%"
                    )
                    previous_percent = percent
                report_progress(job, basename, "Calculating ETag", percent)

        # Get an MD5 hash of the concatenated hashes. Append the number of
        # parts. For example, "c7c2300fd47954c421d5fe0bc7910ca3-64":
        # c7c2300fd47954c421d5fe0bc7910ca3 is the hash of the concatenated
        # MD5 hashes, and the uploaded file had 64 parts (MD5 hashes).

        return (
            hashlib.md5(b"".join(md5_digests)).hexdigest() + "-" + str(len(md5_digests))
        )


def get_matching_s3_objects(bucket, prefix="", suffix=""):
    """Iterate through the objects in S3 storage.

    https://alexwlchan.net/2019/07/listing-s3-keys/ (STE: exempt, licence
    notice)

    Copyright (c) 2012-2019 Alex Chan

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the "Software"),
    to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the Software
    is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
    THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
    OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
    ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
    OTHER DEALINGS IN THE SOFTWARE.
    """

    s3 = aws_s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if isinstance(prefix, str):
        prefixes = (prefix,)
    else:
        prefixes = prefix

    for key_prefix in prefixes:
        kwargs["Prefix"] = key_prefix
        for page in paginator.paginate(**kwargs):
            try:
                contents = page["Contents"]
            except KeyError:
                break
            for obj in contents:
                key = obj["Key"]
                if key.endswith(suffix):
                    yield obj


def sanitize_s3_key(key):
    """Sanitize the key name. Remove the characters that cause problems.

    See https://docs.aws.amazon.com/AmazonS3/latest/dev/UsingMetadata.html
    """


    # fmt: off
    aws_bad_chars  = [   "&",  "$",   "@",  "=", ";", ":", "+", ",", "?", "\\", "{", "^", "}", "%", "`", '"', ">", "~", "<", "#", "|"]
    aws_good_chars = [" and ",  "", " at ", "-", "-", "-", " ",  "",  "",  " ", "(",  "", ")",  "", "'",  "",  "", "-",  "",  "",  ""]
    # fmt: on

    key = os.path.normpath(key)
    key_components = key.split(os.sep)
    key = os.path.join(
        *[
            sanitize_string(component, aws_bad_chars, aws_good_chars)
            for component in key_components
        ]
    )
    return key
