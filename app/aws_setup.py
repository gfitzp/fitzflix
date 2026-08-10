"""Idempotent provisioning of the AWS pieces Fitzflix depends on.

`flask aws provision` drives this. Each step reports what it found and
only creates or updates what's missing, so running it against an already
configured account is safe — existing lifecycle rules and notification
configurations are always preserved and appended to, never replaced.

Because S3 configuration reads are eventually consistent, a read-modify-
write can be handed a stale, partial view and faithfully write it back —
which is how a set of hand-made lifecycle rules was once lost (restored
from a survey taken minutes earlier). Two defenses now apply: the
as-found configuration is saved to a timestamped snapshot file before any
write, and a run that finds fewer lifecycle rules than the newest
snapshot recorded refuses to write without --force.
"""

import glob
import json
import os
import time

import botocore

ABORT_RULE_ID = "fitzflix-abort-incomplete-multipart"
TRANSITION_RULE_ID = "fitzflix-untouched-to-deep-archive"
NOTIFICATION_ID = "fitzflix-restore-completed"

# Failed multipart uploads (a killed backup upload, a dropped connection
# mid-archive) hold invisible, billable parts until aborted; one day gives
# any legitimate retry time to finish

ABORT_AFTER_DAYS = 1

# How long a deleted or replaced original's previous version stays
# recoverable. Deep Archive bills a 180-day minimum storage duration, so
# expiring noncurrent versions any sooner costs exactly the same in
# early-deletion fees — 180 days is the largest recovery window that's
# free relative to any shorter setting

UNTOUCHED_NONCURRENT_RETENTION_DAYS = 180

# Replaced posters churn small noncurrent versions on every change; a
# month is plenty of time to undo a poster mistake

CUSTOM_POSTERS_NONCURRENT_RETENTION_DAYS = 30

CUSTOM_POSTERS_RULE_ID = "fitzflix-custom-posters-noncurrent"


class StaleReadSuspected(Exception):
    """The bucket reported fewer lifecycle rules than the newest snapshot
    knows about — writing now could persist a stale partial read."""


def _snapshot_dir(config):
    return os.path.join(os.path.dirname(config["LOG_FILE"]), "aws-snapshots")


def _save_snapshot(config, payload):
    directory = _snapshot_dir(config)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"lifecycle-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=1, default=str)
    return path


def _newest_snapshot_rule_count(config):
    snapshots = sorted(glob.glob(os.path.join(_snapshot_dir(config), "*.json")))
    if not snapshots:
        return None
    with open(snapshots[-1]) as f:
        return len(json.load(f).get("lifecycle_rules", []))


def provision(config, s3, sqs, echo=print, force=False):
    """Ensure the bucket, its rules, the queue, and the wiring all exist.

    Returns a list of (component, status) pairs, status being "present",
    "created", or "updated".
    """

    bucket = config["AWS_BUCKET"]
    results = []

    def report(component, status):
        results.append((component, status))
        echo(f"{status.upper():>8}  {component}")

    # 1. The bucket itself

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

    # 2. Versioning: the recovery layer for deleted or overwritten objects

    versioning = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    if versioning == "Enabled":
        report("bucket versioning", "present")
    else:
        s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        report("bucket versioning", "created" if versioning is None else "updated")

    # 3. Lifecycle rules, appended to whatever already exists

    try:
        rules = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
            raise
        rules = []

    # Guard against acting on a stale partial read, then record the
    # as-found configuration before anything modifies it

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

    # 3a. A bucket-wide abort for incomplete multipart uploads: the
    # per-prefix rules a bucket may already carry don't cover prefixes
    # added later (backup/, for one), so this rule filters on nothing

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

    # 3b. Transition archived originals to Glacier Deep Archive on arrival.
    # Detected by content, not rule id — an existing hand-made rule counts

    untouched_prefix = f"{config['AWS_UNTOUCHED_PREFIX']}/"

    def transitions_untouched(rule):
        if rule.get("Status") != "Enabled":
            return False
        prefix = rule.get("Filter", {}).get("Prefix", rule.get("Prefix", ""))
        return prefix == untouched_prefix and any(
            transition.get("StorageClass") == "DEEP_ARCHIVE"
            for transition in rule.get("Transitions", [])
        )

    untouched_rule = next((rule for rule in rules if transitions_untouched(rule)), None)
    if untouched_rule is not None:
        report(f"lifecycle: {untouched_prefix} Deep Archive transition", "present")
    else:
        untouched_rule = {
            "ID": TRANSITION_RULE_ID,
            "Status": "Enabled",
            "Filter": {"Prefix": untouched_prefix},
            "Transitions": [{"Days": 0, "StorageClass": "DEEP_ARCHIVE"}],
            "Expiration": {"ExpiredObjectDeleteMarker": True},
        }
        rules.append(untouched_rule)
        changed = True
        report(f"lifecycle: {untouched_prefix} Deep Archive transition", "created")

    # Once a deleted object's noncurrent versions expire, its delete
    # marker is all that remains; expired-marker cleanup removes those.
    # Only set where it wouldn't conflict — AWS forbids combining it with
    # a Days/Date expiration in the same rule

    marker_label = f"lifecycle: {untouched_prefix} expired delete-marker cleanup"
    expiration = untouched_rule.get("Expiration", {})
    if expiration.get("ExpiredObjectDeleteMarker") is True:
        report(marker_label, "present")
    elif "Days" in expiration or "Date" in expiration:
        report(marker_label, "present")
    else:
        untouched_rule["Expiration"] = {"ExpiredObjectDeleteMarker": True}
        changed = True
        report(marker_label, "updated")

    # 3c. Noncurrent-version retention on that rule: versioning is what
    # makes a deleted or replaced original recoverable, and this is how
    # long the recovery window stays open. Raised when shorter, left
    # alone when someone configured longer

    retention_label = (
        f"lifecycle: {untouched_prefix} noncurrent-version retention "
        f"({UNTOUCHED_NONCURRENT_RETENTION_DAYS} days)"
    )
    noncurrent = dict(untouched_rule.get("NoncurrentVersionExpiration", {}))
    current_days = noncurrent.get("NoncurrentDays")
    if current_days is not None and current_days >= UNTOUCHED_NONCURRENT_RETENTION_DAYS:
        report(retention_label, "present")
    else:
        noncurrent["NoncurrentDays"] = UNTOUCHED_NONCURRENT_RETENTION_DAYS
        untouched_rule["NoncurrentVersionExpiration"] = noncurrent
        changed = True
        report(retention_label, "created" if current_days is None else "updated")

    # 3d. Noncurrent-version retention for the custom posters mirror,
    # detected by content so a hand-made rule counts

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

    # 4b. The queue must let S3 deliver to it

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

    # 5. The bucket's restore-completed event notification, merged into
    # whatever notification configurations already exist

    notification = s3.get_bucket_notification_configuration(Bucket=bucket)
    notification.pop("ResponseMetadata", None)
    queue_configurations = notification.get("QueueConfigurations", [])

    def notifies_restore(entry):
        return entry.get(
            "QueueArn"
        ) == queue_arn and "s3:ObjectRestore:Completed" in entry.get("Events", [])

    if any(notifies_restore(entry) for entry in queue_configurations):
        report("bucket restore-completed notification", "present")
    else:
        queue_configurations.append(
            {
                "Id": NOTIFICATION_ID,
                "QueueArn": queue_arn,
                "Events": ["s3:ObjectRestore:Completed"],
                "Filter": {
                    "Key": {
                        "FilterRules": [{"Name": "Prefix", "Value": untouched_prefix}]
                    }
                },
            }
        )
        notification["QueueConfigurations"] = queue_configurations
        s3.put_bucket_notification_configuration(
            Bucket=bucket, NotificationConfiguration=notification
        )
        report("bucket restore-completed notification", "created")

    return results
