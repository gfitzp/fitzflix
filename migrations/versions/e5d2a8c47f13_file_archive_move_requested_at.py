"""Record the request time of a pending archive move on the file row (#618)

The column replaces the Redis hash fitzflix:archive-move:requested. The
move state is aws_key_target and this time, set and cleared together.
The column is nullable. Thus, the old code runs on with it, and the
step can run before the restart. A row with a pending move from before
the step gets its time at its next move request.

Revision ID: e5d2a8c47f13
Revises: b3a9d1e7c4f2
Create Date: 2026-10-30 09:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e5d2a8c47f13"
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
    if "archive_move_requested_at" not in _columns(sa.inspect(bind), "file"):
        op.add_column(
            "file",
            sa.Column("archive_move_requested_at", sa.DateTime(), nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    if "archive_move_requested_at" in _columns(sa.inspect(bind), "file"):
        op.drop_column("file", "archive_move_requested_at")
