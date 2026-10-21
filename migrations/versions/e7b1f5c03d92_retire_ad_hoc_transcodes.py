"""Retire the ad-hoc transcodes (#293)

The SDR conversion of #292 replaces the ad-hoc HandBrake transcodes.
This drops the derived_file table and file.date_transcoded. It also
removes a stored page value of HANDBRAKE_PRESET. That setting no longer
exists.

Revision ID: e7b1f5c03d92
Revises: d4e2a9c71b38
Create Date: 2026-10-20 22:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e7b1f5c03d92"
down_revision = "d4e2a9c71b38"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("ix_derived_file_file_path", table_name="derived_file")
    op.drop_index("ix_derived_file_source_file_id", table_name="derived_file")
    op.drop_table("derived_file")
    with op.batch_alter_table("file", schema=None) as batch_op:
        batch_op.drop_index("ix_file_date_transcoded")
        batch_op.drop_column("date_transcoded")
    op.execute("DELETE FROM app_setting WHERE name = 'HANDBRAKE_PRESET'")


def downgrade():
    with op.batch_alter_table("file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("date_transcoded", sa.DateTime(), nullable=True))
        batch_op.create_index("ix_file_date_transcoded", ["date_transcoded"])
    op.create_table(
        "derived_file",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_file_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=False),
        sa.Column("basename", sa.String(length=255), nullable=False),
        sa.Column("filesize_bytes", sa.BigInteger(), nullable=True),
        sa.Column("date_created", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_file_id"], ["file.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_derived_file_source_file_id", "derived_file", ["source_file_id"]
    )
    op.create_index(
        "ix_derived_file_file_path", "derived_file", ["file_path"], unique=True
    )
