"""Add the pending archive key of a file (#369)

A rename gives the archive of a file a new key. The move job copies the
object to that key, verifies it, and then writes the key to aws_key.
aws_key_target holds the new key while the move is pending.

Revision ID: c4e8a2f61d39
Revises: a9d3e5f17c42
Create Date: 2026-10-25 22:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c4e8a2f61d39"
down_revision = "a9d3e5f17c42"
branch_labels = None
depends_on = None


def upgrade():
    # Run when the queues are idle. MariaDB adds a nullable column at the
    # end of the table without a copy (ALGORITHM=INSTANT). It builds the
    # index in place. The System page and the claim checks read the
    # column. The lock wait stops the ALTER if a long transaction holds
    # the table. Each step checks what is there, because MySQL has no
    # transactional DDL.

    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("file")]
    if "aws_key_target" not in columns:
        op.add_column(
            "file", sa.Column("aws_key_target", sa.String(length=255), nullable=True)
        )
    indexes = [i["name"] for i in sa.inspect(bind).get_indexes("file")]
    if "ix_file_aws_key_target" not in indexes:
        op.create_index("ix_file_aws_key_target", "file", ["aws_key_target"])


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    inspector = sa.inspect(bind)
    if "ix_file_aws_key_target" in [i["name"] for i in inspector.get_indexes("file")]:
        op.drop_index("ix_file_aws_key_target", table_name="file")
    if "aws_key_target" in [c["name"] for c in inspector.get_columns("file")]:
        op.drop_column("file", "aws_key_target")
