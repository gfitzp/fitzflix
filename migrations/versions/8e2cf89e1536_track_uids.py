"""Address the tracks by Matroska UID, and drop the track edit counter (#611)

The audio and subtitle rows get the Matroska track UID. A remux keeps
the UID and changes the positions. Thus, an edit that addresses a track
by UID needs no counter to find a changed position. The upgrade fills
no UID. Run `flask tracks backfill-uids` after the deploy.

Revision ID: 8e2cf89e1536
Revises: d7b1f4a09e52
Create Date: 2026-10-29 18:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "8e2cf89e1536"
down_revision = "d7b1f4a09e52"
branch_labels = None
depends_on = None

TRACK_TABLES = ("file_audio_track", "file_subtitle_track")


def _columns(inspector, table):
    """Return the column names of a table."""

    return [column["name"] for column in inspector.get_columns(table)]


def upgrade():
    # Run when the queues are idle. MariaDB adds a nullable column at the
    # end of a table, and drops a column, without a copy. The lock wait
    # stops the ALTER if a long transaction holds the table. Each step
    # checks what is there, because MySQL has no transactional DDL.

    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    inspector = sa.inspect(bind)
    for table in TRACK_TABLES:
        if "uid" not in _columns(inspector, table):
            op.add_column(table, sa.Column("uid", sa.String(length=20), nullable=True))
    if "track_edit_seq" in _columns(inspector, "file"):
        op.drop_column("file", "track_edit_seq")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    inspector = sa.inspect(bind)
    if "track_edit_seq" not in _columns(inspector, "file"):
        op.add_column("file", sa.Column("track_edit_seq", sa.Integer(), nullable=True))
    for table in TRACK_TABLES:
        if "uid" in _columns(inspector, table):
            op.drop_column(table, "uid")
