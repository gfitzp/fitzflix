"""The key of an archive, and the records that claim a key."""

import os
from flask import current_app
from app import db
from app.archive import s3_layout
from app.media import naming
from app.models import ArchivedOriginal, File
from app.archive import aws_clients

# Supporting functions


def _in_prefix(key, prefix):
    """Return True if an S3 key is below a prefix such as "modified"."""

    return bool(key) and key.startswith(f"{prefix}/")


def _archive_prefix_of(key):
    """Return the archive prefix (untouched, modified, transcoded) that holds key, or None.

    A run-scoped upload of an import belongs to untouched/. Its
    archive move goes there."""

    if is_pending_archive(key):
        return current_app.config["AWS_UNTOUCHED_PREFIX"]
    entry = s3_layout.prefix_of(key)
    if entry is None or entry.lifecycle != "archive":
        return None
    return s3_layout.prefix_value(entry)


def _claim_key(key):
    """Return the key of the claims map for an archive key.

    The map folds letter case, as the key columns do on MariaDB.
    Each writer and each reader of the map goes through this function."""

    return key.casefold()


def key_claims():
    """Return {casefolded key: claimant ids} for each archive key that a record claims.

    A File row claims its aws_key, its aws_transcoded_key and its pending
    aws_key_target. An archive-only original claims its key with the id
    None. The backfill reads this 1 time for all rows. The dict keys fold
    case, as the key columns do on MariaDB. Thus, the batch path
    finds the claims that the SQL path finds."""

    claims = {}
    for row in db.session.query(
        File.id, File.aws_key, File.aws_transcoded_key, File.aws_key_target
    ).all():
        for key in row[1:]:
            if key:
                claims.setdefault(_claim_key(key), set()).add(row[0])
    for (key,) in db.session.query(ArchivedOriginal.aws_key).all():
        claims.setdefault(_claim_key(key), set()).add(None)
    return claims


def _key_claimed(key, exclude_id=None):
    """Return True if a record claims the S3 key.

    A File row claims its aws_key, its aws_transcoded_key and its pending
    aws_key_target. An archive-only original claims its key. exclude_id
    leaves out 1 File row."""

    files = db.session.query(File.id).filter(
        db.or_(
            File.aws_key == key,
            File.aws_transcoded_key == key,
            File.aws_key_target == key,
        )
    )
    if exclude_id is not None:
        files = files.filter(File.id != exclude_id)
    return (
        files.first() is not None
        or db.session.query(ArchivedOriginal.id)
        .filter(ArchivedOriginal.aws_key == key)
        .first()
        is not None
    )


def _claimed_by_another(file, key, claims=None):
    """Return True if a record other than file claims key."""

    if claims is not None:
        return bool(claims.get(_claim_key(key), set()) - {file.id})
    return _key_claimed(key, exclude_id=file.id)


def archive_key_for(file, prefix, claims=None):
    """Return the archive key of a file under prefix.

    The key is the prefix and the sanitized untouched name of the file.
    The untouched name holds the title of the film or the series. The
    basename of an extra is not unique, because many films have a
    "Trailer.mkv". A conversion keeps the quality of its original in its
    untouched name. Its transcoded/ copy has the quality of its library
    file. Thus, the tag changes for each prefix but untouched/.

    Invariant: the extension of a key tells the type of its object
    (#778). An object in untouched/ is the file of the import, and it
    keeps that name. An object in modified/ or transcoded/ is the
    library file. The import changes an MP4 into a Matroska file. Thus,
    such a key gets the extension of the library file.

    A key that the file holds already is its own. If another record
    claims the key, the record id goes into the name. claims is the
    result of key_claims, for a pass over many rows."""

    config = current_app.config
    name = aws_clients.sanitize_s3_key(file.untouched_basename or file.basename)
    if prefix != config["AWS_UNTOUCHED_PREFIX"]:
        if file.converted_from_quality and file.quality:
            name = naming.retag_quality(
                name, file.converted_from_quality, file.quality.quality_title
            )
        extension = os.path.splitext(file.basename or "")[1]
        if extension:
            name = naming.with_extension(name, extension)
    key = os.path.join(prefix, name)
    if key in (file.aws_key, file.aws_transcoded_key, file.aws_key_target):
        return key
    if _claimed_by_another(file, key, claims):
        stem, extension = os.path.splitext(name)
        key = os.path.join(prefix, f"{stem} ({file.id}){extension}")
    return key


def same_archive_key(key, rule_key):
    """Return True if 2 archive keys are equal when letter case is ignored.

    A key holds the year and the untouched rip name. Thus, the keys of 2
    different files never differ only by case. A case-only difference
    is always the same file. Its archive keeps its key and does not
    move. With no case-only moves, the key columns can fold case on
    MariaDB (utf8mb4_general_ci). A plain SQL equality on a key then
    finds the correct claims. sanitize_s3_key runs unidecode, so every
    rule key is ASCII. On ASCII, utf8mb4_general_ci folds only case and
    trailing spaces."""

    return key.casefold() == rule_key.casefold()


def archive_drift(file, claims=None, column=None):
    """Return (key, rule key) of the first archive of a file that has the wrong key.

    The archive in aws_key comes first. Then the transcoded/ copy of a
    conversion. Return None when each archive has the key of its
    untouched name. An archive with no upload does not count. A key that differs from its rule key only by letter case is
    correct (same_archive_key). flask aws converge-keys uses this rule
    too.

    column limits the check to 1 key column, "aws_key" or
    "aws_transcoded_key". A move that is not a converge run moves only
    the archive that it asks for. A lagging key in the other column
    stays for the converge run."""

    config = current_app.config
    if column in (None, "aws_key") and file.aws_key and file.aws_date_uploaded:
        prefix = _archive_prefix_of(file.aws_key)
        if prefix:
            wanted = archive_key_for(file, prefix, claims)
            if not same_archive_key(file.aws_key, wanted):
                return file.aws_key, wanted
    if (
        column in (None, "aws_transcoded_key")
        and file.converted_from_quality
        and file.aws_transcoded_key
        and file.aws_transcoded_date_uploaded
        and _in_prefix(file.aws_transcoded_key, config["AWS_TRANSCODED_PREFIX"])
    ):
        wanted = archive_key_for(file, config["AWS_TRANSCODED_PREFIX"], claims)
        if not same_archive_key(file.aws_transcoded_key, wanted):
            return file.aws_transcoded_key, wanted
    return None


def _target_column(file):
    """Return the name of the key column that the pending target of a file replaces."""

    if _in_prefix(file.aws_key_target, current_app.config["AWS_TRANSCODED_PREFIX"]):
        return "aws_transcoded_key"
    return "aws_key"


def move_source(file):
    """Return the current key of the archive that the pending move of a file moves."""

    if not file.aws_key_target:
        return None
    return getattr(file, _target_column(file))


def untouched_key_still_claimed(key):
    """Return True if a surviving file record still claims this S3 key.

    This applies to the untouched key. Different records can share a
    key. For example, a second import can arrive on the same basename.
    A delete of a
    claimed key would strand the archive of the survivor behind a
    delete marker. The caller must
    check AFTER its own deletes commit. Thus, the purged rows no longer
    count. An archive-only original claims its key too. So does
    the target of a pending move.

    The key columns fold case on MariaDB. A case-only rename keeps its
    key. Thus, a claim can differ from key only by letter case.
    upload_untouched reads the object of the exact key for that case.
    """

    return _key_claimed(key)


# A same-name import whose bytes differ from a claimed untouched/ object
# uploads below this prefix first. The final key changes only
# after the records commit. The prefix is outside untouched/. Thus, the
# day-0 Deep Archive rule of untouched/ never applies to it.

PENDING_ARCHIVE_PREFIX = s3_layout.PREFIX_BY_NAME["pending-untouched"].fixed


def is_pending_archive(key):
    """Return True if key is a run-scoped upload of an import."""

    return _in_prefix(key, PENDING_ARCHIVE_PREFIX)


def pending_archive_of(file_details):
    """Return the run-scoped upload key of an import, or None."""

    key = (file_details or {}).get("aws_key")
    return key if key and is_pending_archive(key) else None


def pending_archive_key(key, run_id):
    """Return the run-scoped key of an import upload for the normal key."""

    prefix = current_app.config["AWS_UNTOUCHED_PREFIX"]
    return os.path.join(PENDING_ARCHIVE_PREFIX, run_id, os.path.relpath(key, prefix))
