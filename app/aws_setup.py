"""Provision the AWS components that Fitzflix depends on. This is idempotent.

`flask aws provision` runs this module. Each step reports what it
found. It creates or updates only the components that are missing.
Thus, a run against an account that is already configured is safe.
The module always keeps the existing lifecycle rules and notification
configurations. It appends to them. It never replaces them.

S3 configuration reads are eventually consistent. Thus, a
read-modify-write step can get a stale, partial view and write it
back. This is how a set of hand-made lifecycle rules was lost 1 time.
Fitzflix restored them from a survey taken some minutes before. Two
defenses now apply. First, the module saves the configuration as found
to a snapshot file with a timestamp before each write. Second, if a
run finds fewer lifecycle rules than the newest snapshot recorded, it
refuses to write without --force.
"""

import glob
import json
import os
import time

import botocore

ABORT_RULE_ID = "fitzflix-abort-incomplete-multipart"
TRANSITION_RULE_ID = "fitzflix-untouched-to-deep-archive"
NOTIFICATION_ID = "fitzflix-restore-completed"

# A failed multipart upload (a killed backup upload, or a dropped
# connection during an archive) holds invisible parts that AWS bills
# until an abort. One day gives a legitimate retry the time to complete

ABORT_AFTER_DAYS = 1

# This is the time that the previous version of a deleted or replaced
# original stays recoverable. Deep Archive bills a minimum storage
# duration of 180 days. Thus, to expire the noncurrent versions sooner
# costs exactly the same in early-deletion fees. 180 days is the largest
# recovery window that is free compared with a shorter setting

UNTOUCHED_NONCURRENT_RETENTION_DAYS = 180

# A replaced poster makes a small noncurrent version on each change. One
# month is sufficient time to undo a poster mistake

CUSTOM_POSTERS_NONCURRENT_RETENTION_DAYS = 30

CUSTOM_POSTERS_RULE_ID = "fitzflix-custom-posters-noncurrent"


class StaleReadSuspected(Exception):
    """The bucket reported fewer lifecycle rules than the newest snapshot.

    A write now could make a stale partial read permanent."""


def _snapshot_dir(config):
    """Return the directory of the provision snapshots, beside the app log."""

    return os.path.join(os.path.dirname(config["LOG_FILE"]), "aws-snapshots")


def _save_snapshot(config, payload):
    """Write the AWS configuration as found to a snapshot file.

    The file name has a timestamp. This function returns the path.
    """

    directory = _snapshot_dir(config)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"lifecycle-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=1, default=str)
    return path


def _newest_snapshot_rule_count(config):
    """Return the lifecycle-rule count that the newest snapshot recorded.

    Return None if no snapshot exists yet.
    """

    snapshots = sorted(glob.glob(os.path.join(_snapshot_dir(config), "*.json")))
    if not snapshots:
        return None
    with open(snapshots[-1]) as f:
        return len(json.load(f).get("lifecycle_rules", []))


def provision(config, s3, sqs, echo=print, force=False):
    """Make sure that the bucket, its rules, the queue, and the wiring exist.

    This function returns a list of (component, status) pairs. The
    status is "present", "created", or "updated".
    """

    bucket = config["AWS_BUCKET"]
    results = []

    def report(component, status):
        results.append((component, status))
        echo(f"{status.upper():>8}  {component}")

    # 1. The bucket

    try:
        s3.head_bucket(Bucket=bucket)
        report(f"bucket {bucket}", "present")
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
        region = s3.meta.region_name
        create_args = {"Bucket": bucket}
        if region and region != "us-east-1":
            create_args["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**create_args)
        report(f"bucket {bucket}", "created")

    # 2. Versioning. This is the recovery layer for deleted or overwritten
    # objects

    versioning = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    if versioning == "Enabled":
        report("bucket versioning", "present")
    else:
        s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        report("bucket versioning", "created" if versioning is None else "updated")

    # 3. The lifecycle rules. Fitzflix appends them to the rules that exist

    try:
        rules = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
            raise
        rules = []

    # Prevent an action on a stale partial read. Then record the
    # configuration as found before a step modifies it

    known_count = _newest_snapshot_rule_count(config)
    if known_count is not None and len(rules) < known_count and not force:
        raise StaleReadSuspected(
            f"The bucket reports {len(rules)} lifecycle rule(s), but the "
            f"newest snapshot recorded {known_count}. This can be an S3 "
            f"eventually-consistent read — retry in a minute, restore from "
            f"the snapshot in {_snapshot_dir(config)}, or re-run with "
            f"--force if the reduction is intentional."
        )
    notification_before = s3.get_bucket_notification_configuration(Bucket=bucket)
    notification_before.pop("ResponseMetadata", None)
    snapshot_path = _save_snapshot(
        config,
        {"lifecycle_rules": rules, "notification": notification_before},
    )
    echo(f"          as-found configuration saved to {snapshot_path}")

    changed = False

    # 3a. A bucket-wide abort for incomplete multipart uploads. A bucket
    # can already have per-prefix rules. Those rules do not cover the
    # prefixes added later (for example backup/). Thus, this rule has no
    # filter

    if any(rule.get("ID") == ABORT_RULE_ID for rule in rules):
        report("lifecycle: bucket-wide incomplete-multipart abort", "present")
    else:
        rules.append(
            {
                "ID": ABORT_RULE_ID,
                "Status": "Enabled",
                "Filter": {},
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": ABORT_AFTER_DAYS
                },
            }
        )
        changed = True
        report("lifecycle: bucket-wide incomplete-multipart abort", "created")

    # 3b. Move the archives to Glacier Deep Archive on arrival, for each
    # archive prefix: untouched/ (the rips), modified/, and transcoded/
    # (#299). Fitzflix detects a rule by content, not by rule id. Thus,
    # an existing hand-made rule counts

    changes = []

    def ensure_archive_rules(prefix, rule_id):
        """Make sure that 1 archive prefix has its transition and retention rules."""

        def transitions_prefix(rule):
            if rule.get("Status") != "Enabled":
                return False
            rule_prefix = rule.get("Filter", {}).get("Prefix", rule.get("Prefix", ""))
            return rule_prefix == prefix and any(
                transition.get("StorageClass") == "DEEP_ARCHIVE"
                for transition in rule.get("Transitions", [])
            )

        rule_for_prefix = next(
            (rule for rule in rules if transitions_prefix(rule)), None
        )
        if rule_for_prefix is not None:
            report(f"lifecycle: {prefix} Deep Archive transition", "present")
        else:
            rule_for_prefix = {
                "ID": rule_id,
                "Status": "Enabled",
                "Filter": {"Prefix": prefix},
                "Transitions": [{"Days": 0, "StorageClass": "DEEP_ARCHIVE"}],
                "Expiration": {"ExpiredObjectDeleteMarker": True},
            }
            rules.append(rule_for_prefix)
            changes.append(prefix)
            report(f"lifecycle: {prefix} Deep Archive transition", "created")

        # After the noncurrent versions of a deleted object expire, only its
        # delete marker remains. The expired-marker cleanup removes those
        # markers. Set it only where it does not conflict. AWS does not permit
        # it together with a Days/Date expiration in the same rule

        marker_label = f"lifecycle: {prefix} expired delete-marker cleanup"
        expiration = rule_for_prefix.get("Expiration", {})
        if expiration.get("ExpiredObjectDeleteMarker") is True:
            report(marker_label, "present")
        elif "Days" in expiration or "Date" in expiration:
            report(marker_label, "present")
        else:
            rule_for_prefix["Expiration"] = {"ExpiredObjectDeleteMarker": True}
            changes.append(prefix)
            report(marker_label, "updated")

        # The noncurrent-version retention on that rule. Versioning makes
        # a deleted or replaced archive recoverable. This value is the time
        # that the recovery window stays open. Fitzflix increases it if it is
        # shorter. It does not change it if a person configured a longer time

        retention_label = (
            f"lifecycle: {prefix} noncurrent-version retention "
            f"({UNTOUCHED_NONCURRENT_RETENTION_DAYS} days)"
        )
        noncurrent = dict(rule_for_prefix.get("NoncurrentVersionExpiration", {}))
        current_days = noncurrent.get("NoncurrentDays")
        if (
            current_days is not None
            and current_days >= UNTOUCHED_NONCURRENT_RETENTION_DAYS
        ):
            report(retention_label, "present")
        else:
            noncurrent["NoncurrentDays"] = UNTOUCHED_NONCURRENT_RETENTION_DAYS
            rule_for_prefix["NoncurrentVersionExpiration"] = noncurrent
            changes.append(prefix)
            report(retention_label, "created" if current_days is None else "updated")

    untouched_prefix = f"{config['AWS_UNTOUCHED_PREFIX']}/"
    archive_prefixes = [
        (untouched_prefix, TRANSITION_RULE_ID),
        (
            f"{config['AWS_MODIFIED_PREFIX']}/",
            f"fitzflix-{config['AWS_MODIFIED_PREFIX']}-to-deep-archive",
        ),
        (
            f"{config['AWS_TRANSCODED_PREFIX']}/",
            f"fitzflix-{config['AWS_TRANSCODED_PREFIX']}-to-deep-archive",
        ),
    ]
    for prefix, rule_id in archive_prefixes:
        ensure_archive_rules(prefix, rule_id)
    if changes:
        changed = True

    # 3d. The noncurrent-version retention for the custom posters mirror.
    # Fitzflix detects it by content. Thus, a hand-made rule counts

    posters_prefix = f"{config['AWS_CUSTOM_POSTERS_PREFIX']}/"
    posters_label = (
        f"lifecycle: {posters_prefix} noncurrent-version retention "
        f"({CUSTOM_POSTERS_NONCURRENT_RETENTION_DAYS} days)"
    )

    def expires_poster_versions(rule):
        prefix = rule.get("Filter", {}).get("Prefix", rule.get("Prefix", ""))
        return (
            rule.get("Status") == "Enabled"
            and prefix == posters_prefix
            and "NoncurrentDays" in rule.get("NoncurrentVersionExpiration", {})
        )

    if any(expires_poster_versions(rule) for rule in rules):
        report(posters_label, "present")
    else:
        rules.append(
            {
                "ID": CUSTOM_POSTERS_RULE_ID,
                "Status": "Enabled",
                "Filter": {"Prefix": posters_prefix},
                "NoncurrentVersionExpiration": {
                    "NoncurrentDays": CUSTOM_POSTERS_NONCURRENT_RETENTION_DAYS
                },
                "Expiration": {"ExpiredObjectDeleteMarker": True},
            }
        )
        changed = True
        report(posters_label, "created")

    if changed:
        s3.put_bucket_lifecycle_configuration(
            Bucket=bucket, LifecycleConfiguration={"Rules": rules}
        )

    # 4. The restore-notification queue

    queue_url = config.get("AWS_SQS_URL")
    if queue_url:
        report("sqs queue", "present")
    else:
        queue_name = f"fitzflix-restore-{config['AWS_UNTOUCHED_PREFIX']}"
        queue_url = sqs.create_queue(QueueName=queue_name)["QueueUrl"]
        report(f"sqs queue {queue_name}", "created")
        echo(f"          add to .env: AWS_SQS_URL={queue_url}")

    attributes = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn", "Policy"]
    )["Attributes"]
    queue_arn = attributes["QueueArn"]
    bucket_arn = f"arn:aws:s3:::{bucket}"

    # 4b. The queue must permit S3 to deliver messages to it

    policy = json.loads(
        attributes.get("Policy") or '{"Version": "2012-10-17", "Statement": []}'
    )
    allows_s3 = any(
        statement.get("Principal", {}).get("Service") == "s3.amazonaws.com"
        and statement.get("Condition", {}).get("ArnLike", {}).get("aws:SourceArn")
        == bucket_arn
        for statement in policy.get("Statement", [])
    )
    if allows_s3:
        report("sqs queue policy allows S3", "present")
    else:
        policy.setdefault("Statement", []).append(
            {
                "Sid": "fitzflix-allow-s3-restore-events",
                "Effect": "Allow",
                "Principal": {"Service": "s3.amazonaws.com"},
                "Action": "sqs:SendMessage",
                "Resource": queue_arn,
                "Condition": {"ArnLike": {"aws:SourceArn": bucket_arn}},
            }
        )
        sqs.set_queue_attributes(
            QueueUrl=queue_url, Attributes={"Policy": json.dumps(policy)}
        )
        report("sqs queue policy allows S3", "updated")

    # 5. The restore-completed event notification of the bucket. Fitzflix
    # merges it into the notification configurations that exist

    notification = s3.get_bucket_notification_configuration(Bucket=bucket)
    notification.pop("ResponseMetadata", None)
    queue_configurations = notification.get("QueueConfigurations", [])

    def notifies_restore(entry, prefix):
        rules = entry.get("Filter", {}).get("Key", {}).get("FilterRules", [])
        return (
            entry.get("QueueArn") == queue_arn
            and "s3:ObjectRestore:Completed" in entry.get("Events", [])
            and {"Name": "Prefix", "Value": prefix} in rules
        )

    added = False
    for prefix, _rule_id in archive_prefixes:
        label = f"bucket restore-completed notification for {prefix}"
        if any(notifies_restore(entry, prefix) for entry in queue_configurations):
            report(label, "present")
            continue
        notification_id = (
            NOTIFICATION_ID
            if prefix == untouched_prefix
            else f"{NOTIFICATION_ID}-{prefix.rstrip('/')}"
        )
        queue_configurations.append(
            {
                "Id": notification_id,
                "QueueArn": queue_arn,
                "Events": ["s3:ObjectRestore:Completed"],
                "Filter": {
                    "Key": {"FilterRules": [{"Name": "Prefix", "Value": prefix}]}
                },
            }
        )
        added = True
        report(label, "created")
    if added:
        notification["QueueConfigurations"] = queue_configurations
        s3.put_bucket_notification_configuration(
            Bucket=bucket, NotificationConfiguration=notification
        )

    return results
