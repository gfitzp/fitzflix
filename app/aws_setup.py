"""Idempotent provisioning of the AWS pieces Fitzflix depends on.

`flask aws provision` drives this. Each step reports what it found and
only creates or updates what's missing, so running it against an already
configured account is safe — existing lifecycle rules and notification
configurations are always preserved and appended to, never replaced.
"""

import json

import botocore

ABORT_RULE_ID = "fitzflix-abort-incomplete-multipart"
TRANSITION_RULE_ID = "fitzflix-untouched-to-deep-archive"
NOTIFICATION_ID = "fitzflix-restore-completed"

# Failed multipart uploads (a killed backup upload, a dropped connection
# mid-archive) hold invisible, billable parts until aborted; one day gives
# any legitimate retry time to finish

ABORT_AFTER_DAYS = 1


def provision(config, s3, sqs, echo=print):
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

    if any(transitions_untouched(rule) for rule in rules):
        report(f"lifecycle: {untouched_prefix} Deep Archive transition", "present")
    else:
        rules.append(
            {
                "ID": TRANSITION_RULE_ID,
                "Status": "Enabled",
                "Filter": {"Prefix": untouched_prefix},
                "Transitions": [{"Days": 0, "StorageClass": "DEEP_ARCHIVE"}],
            }
        )
        changed = True
        report(f"lifecycle: {untouched_prefix} Deep Archive transition", "created")

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
