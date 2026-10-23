"""Rename the archive columns of file and archived_original (#299)

aws_untouched_key of file holds an untouched/ or a modified/ key since
#299. Thus, the aws_untouched_ columns become aws_. The JSON of a held
import keeps its file details. Their keys change the same way.

Revision ID: a9d3e5f17c42
Revises: f2c8d1a4e6b7
Create Date: 2026-10-22 21:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a9d3e5f17c42"
down_revision = "f2c8d1a4e6b7"
branch_labels = None
depends_on = None

FILE_COLUMNS = ["key", "filesize_bytes", "date_uploaded", "date_deleted", "stale"]
ARCHIVED_COLUMNS = ["key", "filesize_bytes", "date_uploaded"]


def _rename(old_prefix, new_prefix, index_renames):
    # Run when the queues are idle (2026-10-22). Each table changes in
    # one ALTER. MariaDB renames a column and an index without a copy of
    # the table. Each step checks what is there, because MySQL has no
    # transactional DDL.

    bind = op.get_bind()
    op.execute("SET SESSION lock_wait_timeout = 10")
    inspector = sa.inspect(bind)
    for table, names in (
        ("file", FILE_COLUMNS),
        ("archived_original", ARCHIVED_COLUMNS),
    ):
        columns = [c["name"] for c in inspector.get_columns(table)]
        indexes = [i["name"] for i in inspector.get_indexes(table)]
        clauses = [
            f"RENAME COLUMN {old_prefix}{name} TO {new_prefix}{name}"
            for name in names
            if f"{old_prefix}{name}" in columns
        ]
        clauses += [
            f"RENAME INDEX {old} TO {new}"
            for (index_table, old, new) in index_renames
            if index_table == table and old in indexes
        ]
        if clauses:
            op.execute(f"ALTER TABLE {table} " + ", ".join(clauses))
    for name in FILE_COLUMNS:
        op.execute(
            f"UPDATE held_import SET file_details = REPLACE(file_details, "
            f"'\"{old_prefix}{name}\"', '\"{new_prefix}{name}\"')"
        )


def upgrade():
    _rename(
        "aws_untouched_",
        "aws_",
        [
            ("file", "ix_file_aws_untouched_key", "ix_file_aws_key"),
            ("archived_original", "aws_untouched_key", "aws_key"),
        ],
    )


def downgrade():
    _rename(
        "aws_",
        "aws_untouched_",
        [
            ("file", "ix_file_aws_key", "ix_file_aws_untouched_key"),
            ("archived_original", "aws_key", "aws_untouched_key"),
        ],
    )
