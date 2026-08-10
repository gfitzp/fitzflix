"""The `flask aws provision` machinery: idempotent creation of the bucket,
lifecycle rules, queue policy, and restore notification — always appending
to existing configuration, never replacing it.
"""

import json

import botocore.exceptions

from app.aws_setup import ABORT_RULE_ID, provision


class FakeMeta:
    region_name = "us-east-1"


class FakeS3:
    def __init__(self, exists=True, versioning=None, rules=None, notification=None):
        self.meta = FakeMeta()
        self.exists = exists
        self.versioning = versioning
        self.rules = rules if rules is not None else []
        self.notification = notification or {}
        self.writes = []

    def _error(self, code, operation):
        return botocore.exceptions.ClientError({"Error": {"Code": code}}, operation)

    def head_bucket(self, Bucket):
        if not self.exists:
            raise self._error("404", "HeadBucket")

    def create_bucket(self, **kwargs):
        self.exists = True
        self.writes.append("create_bucket")

    def get_bucket_versioning(self, Bucket):
        return {"Status": self.versioning} if self.versioning else {}

    def put_bucket_versioning(self, Bucket, VersioningConfiguration):
        self.versioning = VersioningConfiguration["Status"]
        self.writes.append("put_versioning")

    def get_bucket_lifecycle_configuration(self, Bucket):
        if not self.rules:
            raise self._error("NoSuchLifecycleConfiguration", "GetBucketLifecycle")
        return {"Rules": self.rules}

    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):
        self.rules = LifecycleConfiguration["Rules"]
        self.writes.append("put_lifecycle")

    def get_bucket_notification_configuration(self, Bucket):
        return dict(self.notification, ResponseMetadata={})

    def put_bucket_notification_configuration(self, Bucket, NotificationConfiguration):
        self.notification = NotificationConfiguration
        self.writes.append("put_notification")


class FakeSQS:
    def __init__(
        self, arn="arn:aws:sqs:us-east-1:1:fitzflix-restore-untouched", policy=None
    ):
        self.arn = arn
        self.policy = policy
        self.writes = []

    def create_queue(self, QueueName):
        self.writes.append("create_queue")
        return {"QueueUrl": f"https://sqs.test/{QueueName}"}

    def get_queue_attributes(self, QueueUrl, AttributeNames):
        attributes = {"QueueArn": self.arn}
        if self.policy:
            attributes["Policy"] = json.dumps(self.policy)
        return {"Attributes": attributes}

    def set_queue_attributes(self, QueueUrl, Attributes):
        self.policy = json.loads(Attributes["Policy"])
        self.writes.append("set_queue_attributes")


CONFIG = {
    "AWS_BUCKET": "test-bucket",
    "AWS_UNTOUCHED_PREFIX": "untouched",
    "AWS_CUSTOM_POSTERS_PREFIX": "custom-posters",
    "AWS_SQS_URL": "https://sqs.test/existing",
}


def s3_policy_statement(bucket="test-bucket"):
    return {
        "Effect": "Allow",
        "Principal": {"Service": "s3.amazonaws.com"},
        "Action": "sqs:SendMessage",
        "Resource": "arn:aws:sqs:us-east-1:1:fitzflix-restore-untouched",
        "Condition": {"ArnLike": {"aws:SourceArn": f"arn:aws:s3:::{bucket}"}},
    }


def test_provision_creates_everything_from_nothing(tmp_path):
    s3 = FakeS3(exists=False)
    sqs = FakeSQS()
    config = dict(CONFIG, AWS_SQS_URL=None, LOG_FILE=str(tmp_path / "app.log"))

    results = dict(provision(config, s3, sqs, echo=lambda *_: None))

    assert s3.exists
    assert s3.versioning == "Enabled"
    rule_ids = [rule["ID"] for rule in s3.rules]
    assert ABORT_RULE_ID in rule_ids
    untouched_rule = next(
        rule
        for rule in s3.rules
        if rule.get("Filter", {}).get("Prefix") == "untouched/"
    )
    assert untouched_rule["Transitions"][0]["StorageClass"] == "DEEP_ARCHIVE"
    assert untouched_rule["NoncurrentVersionExpiration"]["NoncurrentDays"] == 180
    assert untouched_rule["Expiration"] == {"ExpiredObjectDeleteMarker": True}
    posters_rule = next(
        rule
        for rule in s3.rules
        if rule.get("Filter", {}).get("Prefix") == "custom-posters/"
    )
    assert posters_rule["NoncurrentVersionExpiration"]["NoncurrentDays"] == 30
    assert "create_queue" in sqs.writes
    assert any(
        statement.get("Principal", {}).get("Service") == "s3.amazonaws.com"
        for statement in sqs.policy["Statement"]
    )
    assert s3.notification["QueueConfigurations"][0]["Events"] == [
        "s3:ObjectRestore:Completed"
    ]
    # The delete-marker cleanup is created as part of the untouched/ rule
    # itself, so its own step finds it already present
    statuses = dict(results)
    assert (
        statuses.pop("lifecycle: untouched/ expired delete-marker cleanup") == "present"
    )
    assert all(status in ("created", "updated") for status in statuses.values())


def test_provision_preserves_existing_hand_made_configuration(tmp_path):
    """Against a bucket shaped like the real one — versioning on, per-prefix
    rules with their own names, notification wired — only the bucket-wide
    abort rule is added, and nothing existing is touched."""

    hand_made_rules = [
        {
            "ID": "untouched/ transition to Glacier Deep Archive",
            "Status": "Enabled",
            "Filter": {"Prefix": "untouched/"},
            "Transitions": [{"Days": 0, "StorageClass": "DEEP_ARCHIVE"}],
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            "Expiration": {"ExpiredObjectDeleteMarker": True},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
        },
        {
            "ID": "custom-posters/ lifecycle",
            "Status": "Enabled",
            "Filter": {"Prefix": "custom-posters/"},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
        },
    ]
    notification = {
        "QueueConfigurations": [
            {
                "Id": "Restore completed",
                "QueueArn": "arn:aws:sqs:us-east-1:1:fitzflix-restore-untouched",
                "Events": ["s3:ObjectRestore:Completed"],
                "Filter": {
                    "Key": {"FilterRules": [{"Name": "Prefix", "Value": "untouched/"}]}
                },
            }
        ]
    }
    s3 = FakeS3(
        versioning="Enabled",
        rules=[dict(rule) for rule in hand_made_rules],
        notification=notification,
    )
    sqs = FakeSQS(
        policy={"Version": "2012-10-17", "Statement": [s3_policy_statement()]}
    )

    config = dict(CONFIG, LOG_FILE=str(tmp_path / "app.log"))
    results = dict(provision(config, s3, sqs, echo=lambda *_: None))

    changed = {
        component: status
        for component, status in results.items()
        if status != "present"
    }
    assert changed == {
        "lifecycle: bucket-wide incomplete-multipart abort": "created",
        "lifecycle: untouched/ noncurrent-version retention (180 days)": "updated",
    }

    # The hand-made untouched/ rule keeps every field except the raised
    # retention; the other rule survives verbatim; ours is appended
    untouched = s3.rules[0]
    assert untouched["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert untouched["Expiration"] == {"ExpiredObjectDeleteMarker": True}
    assert untouched["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 1}
    assert untouched["Transitions"] == hand_made_rules[0]["Transitions"]
    assert s3.rules[1] == hand_made_rules[1]
    assert s3.rules[2]["ID"] == ABORT_RULE_ID
    assert s3.rules[2]["Filter"] == {}

    # No notification or queue writes happened at all
    assert "put_notification" not in s3.writes
    assert sqs.writes == []


def test_provision_is_idempotent(tmp_path):
    s3 = FakeS3(exists=False)
    sqs = FakeSQS()
    config = dict(CONFIG, LOG_FILE=str(tmp_path / "app.log"))

    provision(config, s3, sqs, echo=lambda *_: None)
    first_writes = list(s3.writes) + list(sqs.writes)

    results = dict(provision(config, s3, sqs, echo=lambda *_: None))

    assert all(status == "present" for status in results.values())
    assert list(s3.writes) + list(sqs.writes) == first_writes


def test_provision_snapshots_the_as_found_configuration(tmp_path):
    s3 = FakeS3(
        versioning="Enabled", rules=[{"ID": "keep", "Status": "Enabled", "Filter": {}}]
    )
    sqs = FakeSQS(
        policy={"Version": "2012-10-17", "Statement": [s3_policy_statement()]}
    )
    config = dict(CONFIG, LOG_FILE=str(tmp_path / "app.log"))

    provision(config, s3, sqs, echo=lambda *_: None)

    snapshots = list((tmp_path / "aws-snapshots").glob("lifecycle-*.json"))
    assert len(snapshots) == 1
    saved = json.loads(snapshots[0].read_text())
    assert saved["lifecycle_rules"][0]["ID"] == "keep"
    assert "notification" in saved


def test_provision_refuses_a_suspected_stale_read(tmp_path):
    """A run that sees fewer rules than the newest snapshot recorded must
    not write — an eventually-consistent partial read written back is how
    hand-made rules get silently destroyed."""

    import pytest

    from app.aws_setup import StaleReadSuspected

    config = dict(CONFIG, LOG_FILE=str(tmp_path / "app.log"))
    sqs = FakeSQS(
        policy={"Version": "2012-10-17", "Statement": [s3_policy_statement()]}
    )

    full = FakeS3(
        versioning="Enabled",
        rules=[
            {"ID": f"rule-{n}", "Status": "Enabled", "Filter": {"Prefix": f"p{n}/"}}
            for n in range(3)
        ],
    )
    provision(config, full, sqs, echo=lambda *_: None)

    stale = FakeS3(
        versioning="Enabled",
        rules=[{"ID": "rule-0", "Status": "Enabled", "Filter": {"Prefix": "p0/"}}],
    )
    with pytest.raises(StaleReadSuspected):
        provision(config, stale, sqs, echo=lambda *_: None)
    assert "put_lifecycle" not in stale.writes

    # --force acknowledges an intentional reduction and proceeds
    results = dict(provision(config, stale, sqs, echo=lambda *_: None, force=True))
    assert "put_lifecycle" in stale.writes
    assert any(status != "present" for status in results.values())
