"""Add the track edit counter of a file (#589)

A transient retry of a track edit carries the counter from its first
attempt. A newer edit adds 1 to the counter, and the retry then stops.
A scan does not change the counter.

Revision ID: d7b1f4a09e52
Revises: c4e8a2f61d39
Create Date: 2026-10-29 09:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d7b1f4a09e52"
down_revision = "c4e8a2f61d39"
branch_labels = None
depends_on = None


def upgrade():
    # Run when the queues are idle. MariaDB adds a nullable INT at the
    # end of the table without a copy (ALGORITHM=INSTANT). The track
    # edits and their retries read the column. The lock wait stops the
    # ALTER if a long transaction holds the table. The step checks what
    # is there, because MySQL has no transactional DDL.

    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("file")]
    if "track_edit_seq" not in columns:
        op.add_column("file", sa.Column("track_edit_seq", sa.Integer(), nullable=True))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    inspector = sa.inspect(bind)
    if "track_edit_seq" in [c["name"] for c in inspector.get_columns("file")]:
        op.drop_column("file", "track_edit_seq")
