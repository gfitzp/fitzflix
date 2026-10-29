"""Add the Matroska track UID to the audio and subtitle rows (#611)

A remux keeps the UID and changes the positions. Thus, an edit that
addresses a track by UID needs no counter to find a changed position.
The upgrade fills no UID. A one-time scan of each library file filled
them on Oct 29 2026, while the old code still ran. The next revision
drops the counter column, after the restart.

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
    # MariaDB adds a nullable column at the end of a table without a
    # copy. The lock wait stops the ALTER if a long transaction holds the
    # table. Each step checks what is there, because MySQL has no
    # transactional DDL. The old code never reads the new columns. Thus,
    # this step runs while the old code serves.

    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    inspector = sa.inspect(bind)
    for table in TRACK_TABLES:
        if "uid" not in _columns(inspector, table):
            op.add_column(table, sa.Column("uid", sa.String(length=20), nullable=True))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("SET SESSION lock_wait_timeout = 10")
    inspector = sa.inspect(bind)
    for table in TRACK_TABLES:
        if "uid" in _columns(inspector, table):
            op.drop_column(table, "uid")
