"""Drop the whole-kHz sampling rate of an audio track (#725)

A track scan stored the sampling rate 2 times: sampling_rate in Hz, and
sampling_rate_khz as the whole number of kHz. The page read only the
kHz value, so a track of 44,100 Hz showed "44 kHz". The label now comes
from the Hz value, and nothing reads the kHz column. Each row has its
Hz value, so the step loses no information.

The downgrade computes the column again from sampling_rate, with the
same rounding (the whole number of kHz).

Run the step with the processes stopped, before the start of the new
code. The old code names the column in its queries and in its inserts.

Revision ID: c2e7a9d4f618
Revises: b8d4f2a7c315
Create Date: 2026-11-12 15:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c2e7a9d4f618"
down_revision = "b8d4f2a7c315"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("file_audio_track") as batch:
        batch.drop_column("sampling_rate_khz")


def downgrade():
    with op.batch_alter_table("file_audio_track") as batch:
        batch.add_column(sa.Column("sampling_rate_khz", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE file_audio_track "
        "SET sampling_rate_khz = FLOOR(sampling_rate / 1000) "
        "WHERE sampling_rate IS NOT NULL"
    )
