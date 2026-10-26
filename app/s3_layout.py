"""Hold the layout of the S3 bucket: 1 table entry for each key prefix.

The bucket keeps versions. Thus, a delete without a version id only
adds a delete marker, and the old version stays and costs storage. Each
prefix must free that storage in 1 of 2 ways. A lifecycle rule expires
its noncurrent versions, or the code deletes by version id (#318). The
table records which way applies, the lifecycle rule ids that aws_setup
provisions, and if a restore of the prefix sends a notification.

delete="marker" means a plain delete. The lifecycle rule of the prefix
expires the old version later. Until then, the old version can be
recovered. delete="versions" means that the code removes each version
and each delete marker itself.
"""

from collections import namedtuple

from flask import current_app

# lifecycle names the rule set that aws_setup provisions: "archive"
# (Deep Archive on arrival, 180-day noncurrent retention), "noncurrent"
# (noncurrent_days of retention), "scratch" (an expiry of the current and
# the noncurrent objects), or None.

Prefix = namedtuple(
    "Prefix",
    "name config_key fixed lifecycle rule_ids noncurrent_days "
    "restore_notification delete",
    defaults=(None, None, None, (), None, False, "marker"),
)

PREFIXES = (
    # The archives of the rips. A deleted or replaced original stays
    # recoverable for 180 days. Deep Archive bills a minimum storage of
    # 180 days. Thus, a shorter window costs the same in early-deletion
    # fees, and 180 days is the largest free window.
    #
    # move_archive_to_modified_task deletes the
    # old untouched/ version permanently (by version id). The ETag-equal
    # bytes are in modified/, and a marker would keep a second copy for
    # 180 days for no use. move_archive_object (#369) does the same after
    # a copy with an equal ETag. A copy that only the size proves gets a
    # plain delete.
    Prefix(
        "untouched",
        config_key="AWS_UNTOUCHED_PREFIX",
        lifecycle="archive",
        rule_ids=("fitzflix-untouched-to-deep-archive",),
        noncurrent_days=180,
        restore_notification=True,
    ),
    # The archives of the changed library files and of the conversions
    # (#299). The rule id carries the prefix.
    Prefix(
        "modified",
        config_key="AWS_MODIFIED_PREFIX",
        lifecycle="archive",
        rule_ids=("fitzflix-{prefix}-to-deep-archive",),
        noncurrent_days=180,
        restore_notification=True,
    ),
    Prefix(
        "transcoded",
        config_key="AWS_TRANSCODED_PREFIX",
        lifecycle="archive",
        rule_ids=("fitzflix-{prefix}-to-deep-archive",),
        noncurrent_days=180,
        restore_notification=True,
    ),
    # The mirror of the custom posters. A replaced poster stays 1 month.
    # That is sufficient time to undo a poster mistake.
    Prefix(
        "custom-posters",
        config_key="AWS_CUSTOM_POSTERS_PREFIX",
        lifecycle="noncurrent",
        rule_ids=("fitzflix-custom-posters-noncurrent",),
        noncurrent_days=30,
    ),
    # The scratch area of the Atmos pipeline: the DAMF masters and the
    # .ec3 twins. The run deletes by version. A plain delete left 1.6 TB
    # of noncurrent versions on 2026-10-24. The lifecycle rules remove
    # what a failed run leaves.
    Prefix(
        "mediaconvert-scratch",
        config_key="AWS_MEDIACONVERT_PREFIX",
        lifecycle="scratch",
        rule_ids=(
            "fitzflix-mediaconvert-scratch-expire",
            "fitzflix-mediaconvert-scratch-markers",
        ),
        noncurrent_days=1,
        delete="versions",
    ),
    # The nightly database dumps. The backup task deletes the old versions
    # by version id (#318).
    Prefix("backup", config_key="AWS_BACKUP_PREFIX", delete="versions"),
    # The weekly CSV of the best files for an S3 Batch restore. Each upload
    # replaces the 1 object. The sync removes its old versions.
    Prefix("inventory", fixed="inventory", delete="versions"),
)

PREFIX_BY_NAME = {entry.name: entry for entry in PREFIXES}

INVENTORY_BASENAME = "rank_1.csv"


def prefix_value(entry, config=None):
    """Return the prefix of a table entry, without the slash, from the config."""

    if entry.fixed:
        return entry.fixed
    return (config if config is not None else current_app.config)[entry.config_key]


def prefix_of(key, config=None):
    """Return the table entry of the prefix that holds key, or None."""

    for entry in PREFIXES:
        if key and key.startswith(f"{prefix_value(entry, config)}/"):
            return entry
    return None


def key_under(name, basename, config=None):
    """Return the S3 key of basename under the prefix called name."""

    return f"{prefix_value(PREFIX_BY_NAME[name], config)}/{basename}"


def delete_versions(client, bucket, objects):
    """Delete a list of {"Key", "VersionId"} entries, 1000 to a request."""

    for start in range(0, len(objects), 1000):
        client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": objects[start : start + 1000], "Quiet": True},
        )


def _versions_of(client, bucket, key):
    """Return every version and delete marker of exactly key."""

    found = []
    paginator = client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=key):
        for entry in page.get("Versions", []) + page.get("DeleteMarkers", []):
            if entry["Key"] == key:
                found.append(entry)
    return found


def delete(client, bucket, key, version_id=None):
    """Delete key with the policy of its prefix.

    A version_id deletes that 1 version permanently, whatever the
    policy. A "versions" prefix loses every version of the key. A
    "marker" prefix gets a plain delete, and its lifecycle rule expires
    the old version."""

    if version_id:
        client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
        return
    entry = prefix_of(key)
    if entry is not None and entry.delete == "versions":
        delete_versions(
            client,
            bucket,
            [
                {"Key": found["Key"], "VersionId": found["VersionId"]}
                for found in _versions_of(client, bucket, key)
            ],
        )
        return
    client.delete_object(Bucket=bucket, Key=key)


def prune_noncurrent(client, bucket, key):
    """Delete the noncurrent versions and the delete markers of 1 key.

    The current version stays. A replaced object of a "versions" prefix
    (the inventory) calls this after each upload. Return the count."""

    doomed = [
        {"Key": found["Key"], "VersionId": found["VersionId"]}
        for found in _versions_of(client, bucket, key)
        if not found.get("IsLatest")
    ]
    delete_versions(client, bucket, doomed)
    return len(doomed)
