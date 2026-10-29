"""Record a pending restore on the file row (#610)

The column replaces the Redis restore marker. A writer of the library
file or of its archive refuses while it is set. The column is nullable.
Thus, the old code runs on with it, and the step can run before the
restart.

Revision ID: c4e8f2a6d1b9
Revises: b3a9d1e7c4f2
Create Date: 2026-10-29 21:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c4e8f2a6d1b9"
down_revision = "b3a9d1e7c4f2"
branch_labels = None
depends_on = None


def _columns(inspector, table):
    """Return the column names of a table."""

    return [column["name"] for column in inspector.get_columns(table)]


def upgrade():
    # MariaDB adds a nullable column without a copy. The lock wait stops
    # the ALTER if a long transaction holds the table. The step checks
    # what is there, because MySQL has no transactional DDL.

    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    if "restore_requested_at" not in _columns(sa.inspect(bind), "file"):
        op.add_column(
            "file", sa.Column("restore_requested_at", sa.DateTime(), nullable=True)
        )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    if "restore_requested_at" in _columns(sa.inspect(bind), "file"):
        op.drop_column("file", "restore_requested_at")
