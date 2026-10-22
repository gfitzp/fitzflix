"""The transcoded/ archive of a conversion (#299)

A conversion keeps its original in untouched/. The new fields hold its
second archive: the current 1080p library file in transcoded/. A restore
of a missing conversion uses it first.

Revision ID: f2c8d1a4e6b7
Revises: e7b1f5c03d92
Create Date: 2026-10-22 13:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f2c8d1a4e6b7"
down_revision = "e7b1f5c03d92"
branch_labels = None
depends_on = None


def upgrade():
    # Fail after 10 s if a running job holds a transaction on file. Run
    # the migration when the queues are idle.

    if op.get_bind().dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    with op.batch_alter_table("file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("aws_transcoded_key", sa.String(length=255)))
        batch_op.add_column(
            sa.Column("aws_transcoded_filesize_bytes", sa.BigInteger())
        )
        batch_op.add_column(sa.Column("aws_transcoded_date_uploaded", sa.DateTime()))
        batch_op.create_index("ix_file_aws_transcoded_key", ["aws_transcoded_key"])


def downgrade():
    with op.batch_alter_table("file", schema=None) as batch_op:
        batch_op.drop_index("ix_file_aws_transcoded_key")
        batch_op.drop_column("aws_transcoded_date_uploaded")
        batch_op.drop_column("aws_transcoded_filesize_bytes")
        batch_op.drop_column("aws_transcoded_key")
