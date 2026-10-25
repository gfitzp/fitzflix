"""Test the S3 prefix table of app/s3_layout.py.

The bucket keeps versions. A prefix that has no lifecycle expiry and no
delete by version id keeps every old version forever. That was #318:
the backup prefix grew by 1 dump each night.
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_every_prefix_frees_its_old_versions():
    """Each prefix has a lifecycle expiry or deletes by version id (#318)."""

    from app.s3_layout import PREFIXES

    for entry in PREFIXES:
        expires = entry.lifecycle is not None and entry.noncurrent_days
        assert expires or entry.delete == "versions", entry.name
        assert entry.delete in ("marker", "versions"), entry.name
        assert bool(entry.config_key) != bool(entry.fixed), entry.name
        if entry.lifecycle:
            assert entry.rule_ids, entry.name


def test_every_prefix_that_the_code_writes_to_is_in_the_table(app):
    """Each S3 prefix setting and each literal key of the code is a table prefix."""

    from app.s3_layout import PREFIXES, prefix_of

    config_keys = {entry.config_key for entry in PREFIXES if entry.config_key}
    used = set()
    literal_keys = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        text = path.read_text()
        used.update(re.findall(r"\b(AWS_[A-Z_]+_PREFIX)\b", text))
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if (
                        keyword.arg in ("Key", "Prefix")
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        literal_keys.append((path.name, keyword.value.value))
    assert used <= config_keys, used - config_keys
    with app.app_context():
        for name, key in literal_keys:
            assert prefix_of(key) is not None, (name, key)


class FakeClient:
    """An S3 client that records the deletes and serves 1 version listing."""

    def __init__(self, versions=()):
        self.versions = list(versions)
        self.calls = []

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))

    def delete_objects(self, Bucket, Delete):
        self.calls.append(("delete_objects", Delete["Objects"]))

    def get_paginator(self, name):
        assert name == "list_object_versions"
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                return [
                    {
                        "Versions": [
                            v
                            for v in client.versions
                            if v["Key"].startswith(Prefix) and not v.get("marker")
                        ],
                        "DeleteMarkers": [
                            v
                            for v in client.versions
                            if v["Key"].startswith(Prefix) and v.get("marker")
                        ],
                    }
                ]

        return Paginator()


def test_delete_applies_the_policy_of_the_prefix(app):
    from app import s3_layout

    with app.app_context():
        # An archive prefix gets a plain delete. Its lifecycle rule
        # expires the old version after 180 days.

        client = FakeClient()
        s3_layout.delete(client, "bucket", "untouched/Film.mkv")
        assert client.calls == [
            ("delete_object", {"Bucket": "bucket", "Key": "untouched/Film.mkv"})
        ]

        # A version id deletes that version permanently.

        client = FakeClient()
        s3_layout.delete(client, "bucket", "untouched/Film.mkv", "v1")
        assert client.calls == [
            (
                "delete_object",
                {"Bucket": "bucket", "Key": "untouched/Film.mkv", "VersionId": "v1"},
            )
        ]

        # A "versions" prefix loses every version and marker of the key,
        # and only of that key.

        client = FakeClient(
            [
                {"Key": "backup/a.sql.gz", "VersionId": "1", "IsLatest": True},
                {"Key": "backup/a.sql.gz", "VersionId": "2"},
                {"Key": "backup/a.sql.gz", "VersionId": "3", "marker": True},
                {"Key": "backup/a.sql.gz.old", "VersionId": "4"},
            ]
        )
        s3_layout.delete(client, "bucket", "backup/a.sql.gz")
        assert client.calls == [
            (
                "delete_objects",
                [
                    {"Key": "backup/a.sql.gz", "VersionId": "1"},
                    {"Key": "backup/a.sql.gz", "VersionId": "2"},
                    {"Key": "backup/a.sql.gz", "VersionId": "3"},
                ],
            )
        ]


def test_prune_noncurrent_keeps_the_current_inventory(app):
    from app import s3_layout

    with app.app_context():
        key = s3_layout.key_under("inventory", s3_layout.INVENTORY_BASENAME)
        assert key == "inventory/rank_1.csv"
        client = FakeClient(
            [
                {"Key": key, "VersionId": "new", "IsLatest": True},
                {"Key": key, "VersionId": "old"},
            ]
        )
        assert s3_layout.prune_noncurrent(client, "bucket", key) == 1
        assert client.calls == [("delete_objects", [{"Key": key, "VersionId": "old"}])]


def test_provision_reads_its_rules_from_the_table(tmp_path):
    """Each lifecycle entry of the table gets its rules, by its rule ids."""

    from app.s3_layout import PREFIXES
    from tests.test_aws_provision import CONFIG, FakeS3, FakeSQS

    from app.aws_setup import provision

    s3 = FakeS3(exists=False)
    config = dict(CONFIG, LOG_FILE=str(tmp_path / "app.log"))
    provision(config, s3, FakeSQS(), echo=lambda *_: None)
    rule_ids = {rule["ID"] for rule in s3.rules}
    for entry in PREFIXES:
        prefix = config.get(entry.config_key) if entry.config_key else entry.fixed
        for rule_id in entry.rule_ids:
            assert rule_id.format(prefix=prefix) in rule_ids, entry.name
