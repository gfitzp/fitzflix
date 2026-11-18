"""The download of an archive through CloudFront, and the record of a download that stopped part way."""

import os
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests
from flask import current_app
from app.settings import get_setting

# CloudFront answers with these codes for a fault that a later attempt can
# clear: throttling, and a fault at the edge or at the origin.

RETRYABLE_CDN_STATUS_CODES = (429, 500, 502, 503, 504)

CDN_CHUNK_SIZE = 1024 * 1024

# The connect timeout and the read timeout of one CloudFront request, in
# seconds. A read timeout counts the wait for the next chunk, not the
# whole transfer.

CDN_TIMEOUT = (10, 120)

# This is a seam for the tests. The CloudFront transport fetches through
# this module attribute.

CDN_HTTP_GET = requests.get


class CdnDownloadError(Exception):
    """CloudFront refused a signed-URL download with an HTTP error status."""

    def __init__(self, status_code):
        super().__init__(f"CloudFront answered HTTP {status_code}")
        self.status_code = status_code


def cdn_download_enabled():
    """Return True if the restore downloads must go through CloudFront."""

    return bool(get_setting("AWS_DOWNLOAD_VIA_CDN"))


def missing_cdn_settings():
    """Return the names of the CloudFront settings that are not set."""

    return [
        name
        for name in ("CDN_DOMAIN", "CDN_KEY_PAIR_ID", "CDN_PRIVATE_KEY")
        if not current_app.config.get(name)
    ]


def cdn_settings_problem():
    """Return what is wrong with the CloudFront settings, or None.

    A setting that is not set, or a private key that this process cannot
    read or parse, is a configuration fault. The caller fails fast on it.
    A retry cannot clear it, and the redaction filter blanks the path of
    the key from a traceback (its name ends in KEY)."""

    missing = missing_cdn_settings()
    if missing:
        return f"{', '.join(missing)} not set"
    path = current_app.config["CDN_PRIVATE_KEY"]
    try:
        _cdn_private_key(path)
    except OSError as error:
        return f"CDN_PRIVATE_KEY cannot be read ({error.strerror})"
    except ValueError as error:
        return f"CDN_PRIVATE_KEY does not parse as a PEM private key ({error})"
    return None


_CDN_KEY_CACHE = {}


def _cdn_private_key(path):
    """Return the parsed private key, read from disk one time per change.

    Each download attempt signs a new URL. The key is parsed again only
    when the file changes."""

    from cryptography.hazmat.primitives import serialization

    stamp = os.stat(path).st_mtime_ns
    cached = _CDN_KEY_CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    with open(path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(key_file.read(), password=None)
    _CDN_KEY_CACHE[path] = (stamp, private_key)
    return private_key


def cdn_signed_url(key, expires_in=None):
    """Sign a CloudFront URL for one object key.

    The URL is valid for expires_in seconds (default CDN_URL_EXPIRY). The
    signature is a canned policy that the local private key signs. Thus,
    the key never leaves this machine. Each download attempt signs a new
    URL. Thus, a short expiry costs nothing.
    """

    from botocore.signers import CloudFrontSigner
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    if expires_in is None:
        expires_in = current_app.config["CDN_URL_EXPIRY"]

    private_key = _cdn_private_key(current_app.config["CDN_PRIVATE_KEY"])

    # CloudFront accepts only RSA-SHA1 (PKCS#1 v1.5) signatures on a
    # signed URL. The signature protects a URL with a short life. It does
    # not protect stored data.

    def rsa_signer(message):
        return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())

    signer = CloudFrontSigner(current_app.config["CDN_KEY_PAIR_ID"], rsa_signer)
    url = f"https://{current_app.config['CDN_DOMAIN']}/{urllib.parse.quote(key)}"
    return signer.generate_presigned_url(
        url,
        date_less_than=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    )


def cdn_download(key, destination, progress, expected_size=0, etag=None):
    """Fetch one object through CloudFront, and write it to destination.

    The progress callback gets the size of each chunk, as the boto3
    transfer callback does. A partial file at the destination from an
    earlier attempt is resumed with a Range request when the S3 size is
    known. Thus, a transport fault does not restart a large object from
    byte zero. A sidecar file holds the ETag of the object that the
    partial file came from. A resume needs the same ETag. Thus, a partial
    file of an older version of the key starts again from byte zero.
    A resume keeps the sidecar as it is. Raise CdnDownloadError for an HTTP
    error status. Raise RuntimeError if the body length differs from
    the Content-Length header or from expected_size (the object size
    from the S3 API).
    Raise RuntimeError if neither size is known. The requests transport
    errors escape as they are.

    A resume sends If-Range with the ETag. A response whose ETag is not
    etag removes the partial and raises CdnStaleObject. The
    caller then changes to the S3 transfer.
    """

    url = cdn_signed_url(key)
    have = _partial_size(destination)
    headers = {}
    same_object = bool(etag) and _partial_etag(destination) == etag
    if have and expected_size and have < expected_size and same_object:
        # If-Range makes CloudFront send the whole object (200) when its
        # copy is not the version of the partial.
        headers["Range"] = f"bytes={have}-"
        headers["If-Range"] = f'"{_bare_etag(etag)}"'
    else:
        have = 0

    # The signed URL is a bearer credential while it is valid. No log
    # line here holds it. A transport error from requests names the URL
    # in its message. The SecretRedactor filter on the app logger blanks
    # the Signature parameter of each record.

    with CDN_HTTP_GET(
        url, stream=True, timeout=CDN_TIMEOUT, headers=headers
    ) as response:
        # CloudFront can serve a cached copy of an older version of the
        # key with the same length. The S3 HEAD names the version to
        # place. A different ETag refuses the body.
        served = _bare_etag(response.headers.get("ETag"))
        if (
            response.status_code in (200, 206)
            and served
            and etag
            and served != _bare_etag(etag)
        ):
            _discard_partial(destination)
            raise CdnStaleObject(
                f"CloudFront sent the ETag {served} for '{key}', S3 says "
                f"{_bare_etag(etag)}"
            )
        if response.status_code == 206 and have:
            mode, written = "ab", have
            progress(have)
            # The orphan sweep reads the age of the sidecar. A resumed
            # chain keeps it fresh.
            _touch_partial_etag(destination)
        elif response.status_code == 200:
            mode, written, have = "wb", 0, 0
            _write_partial_etag(destination, etag, expected_size)
        else:
            raise CdnDownloadError(response.status_code)
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length:
            content_length += have
        with open(destination, mode) as output:
            for chunk in response.iter_content(chunk_size=CDN_CHUNK_SIZE):
                output.write(chunk)
                written += len(chunk)
                progress(len(chunk))

    # A body that ends early without a transport error must not become a
    # visible import. The S3 size catches it when the header is absent.
    # With no size known at all, the body cannot be trusted.

    if not content_length and not expected_size:
        raise RuntimeError(f"CloudFront sent {written} bytes for '{key}', size unknown")
    for label, size in (("Content-Length", content_length), ("S3", expected_size)):
        if size and written != size:
            raise RuntimeError(
                f"CloudFront sent {written} bytes for '{key}', {label} says {size}"
            )
    _remove_partial_etag(destination)


class CdnStaleObject(RuntimeError):
    """CloudFront sent a version of the key that is not the version of the S3 HEAD.

    The partial download is gone. aws_download continues with the S3
    transfer."""


def _bare_etag(etag):
    """Return an ETag with no weak prefix and no quotes, or None for a blank value."""

    value = (etag or "").strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"') or None


def _partial_size(destination):
    """Return the size of a partial download at the destination, or 0."""

    try:
        return os.path.getsize(destination)
    except OSError:
        return 0


def read_partial_sidecar(destination):
    """Return (etag, object size) from the sidecar of a download.

    Each value is None when it is not known. The first line is the ETag.
    The second line is the object size, if the download knew it. An
    old sidecar can have a third line. It has no use now."""

    try:
        with open(f"{destination}.etag") as sidecar:
            lines = sidecar.read().split("\n")
    except OSError:
        return None, None
    etag = lines[0].strip() or None
    try:
        size = int(lines[1]) if len(lines) > 1 and lines[1].strip() else None
    except ValueError:
        size = None
    return etag, size


def _partial_etag(destination):
    """Return the ETag of the object that a partial download came from, or None."""

    return read_partial_sidecar(destination)[0]


def _write_partial_etag(destination, etag, size=None):
    """Record the ETag and the size of the object that a download writes to destination.

    The orphan sweep compares the size with the file. A complete copy
    whose sidecar stayed is then not a partial."""

    if not etag:
        _remove_partial_etag(destination)
        return
    lines = [etag, str(int(size)) if size else ""]
    try:
        with open(f"{destination}.etag", "w") as sidecar:
            sidecar.write("\n".join(lines).rstrip("\n"))
    except OSError:
        pass


def _touch_partial_etag(destination):
    """Renew the modification time of the sidecar of a resumed download."""

    try:
        os.utime(f"{destination}.etag")
    except OSError:
        pass


def _remove_partial_etag(destination):
    """Remove the ETag sidecar of a download, if there is one."""

    try:
        os.remove(f"{destination}.etag")
    except FileNotFoundError:
        pass
    except OSError as error:
        current_app.logger.warning(
            f"'{destination}' Could not remove the ETag sidecar: {error}"
        )


def _finished_download(destination, progress):
    """Return True if destination holds the whole object that progress describes.

    The size and the ETag of its sidecar must match the head of the
    object. A newer archive of the key then downloads again."""

    try:
        size = os.path.getsize(destination)
    except OSError:
        return False
    return bool(
        progress.etag
        and size == progress.size
        and _partial_etag(destination) == progress.etag
    )


def _discard_partial(destination):
    """Remove a partial download. The import scan skips dotfiles, so an
    abandoned partial would stay invisible in the import directory."""

    try:
        os.remove(destination)
    except OSError:
        pass
    _remove_partial_etag(destination)
