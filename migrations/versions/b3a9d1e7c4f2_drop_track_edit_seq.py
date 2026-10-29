"""Drop the track edit counter of a file (#611)

The edits address the tracks by Matroska UID since 8e2cf89e1536. The
counter has no reader. The old code reads it. Thus, this step runs
after the restart, when the queues are idle.

Revision ID: b3a9d1e7c4f2
Revises: 8e2cf89e1536
Create Date: 2026-10-29 18:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b3a9d1e7c4f2"
down_revision = "8e2cf89e1536"
branch_labels = None
depends_on = None


def _columns(inspector, table):
    """Return the column names of a table."""

    return [column["name"] for column in inspector.get_columns(table)]


def upgrade():
    # MariaDB drops a column without a copy. The lock wait stops the
    # ALTER if a long transaction holds the table. The step checks what
    # is there, because MySQL has no transactional DDL.

    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    if "track_edit_seq" in _columns(sa.inspect(bind), "file"):
        op.drop_column("file", "track_edit_seq")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    if "track_edit_seq" not in _columns(sa.inspect(bind), "file"):
        op.add_column("file", sa.Column("track_edit_seq", sa.Integer(), nullable=True))
