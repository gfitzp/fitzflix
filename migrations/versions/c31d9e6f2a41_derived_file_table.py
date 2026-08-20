"""Derived files (#19): transcodes tracked in their own table.

Revision ID: c31d9e6f2a41
Revises: 4b74cc0aaecc
Create Date: 2026-08-19 20:05:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c31d9e6f2a41"
down_revision = "4b74cc0aaecc"
branch_labels = None
depends_on = None


def upgrade():
    """Create the derived_file table: source-linked transcode copies,
    rows cascading away with their source File."""

    op.create_table(
        "derived_file",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_file_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=False),
        sa.Column("basename", sa.String(length=255), nullable=False),
        sa.Column("filesize_bytes", sa.BigInteger(), nullable=True),
        # No server default: the ORM supplies utc_timestamp() on insert,
        # matching how File.date_added is populated
        sa.Column("date_created", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_file_id"], ["file.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_derived_file_source_file_id"),
        "derived_file",
        ["source_file_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_derived_file_file_path"), "derived_file", ["file_path"], unique=True
    )


def downgrade():
    """Drop the derived_file table."""

    op.drop_index(op.f("ix_derived_file_file_path"), table_name="derived_file")
    op.drop_index(op.f("ix_derived_file_source_file_id"), table_name="derived_file")
    op.drop_table("derived_file")
