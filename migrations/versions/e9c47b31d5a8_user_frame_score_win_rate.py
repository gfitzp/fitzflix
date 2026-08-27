"""Name that Frame win rates: frames seen and frames won.

Revision ID: e9c47b31d5a8
Revises: d7f3a8b21c94
Create Date: 2026-08-27 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e9c47b31d5a8"
down_revision = "d7f3a8b21c94"
branch_labels = None
depends_on = None


def upgrade():
    """Add user_frame_score.rounds_seen / rounds_won — every dealt
    frame counts as seen (skips included), only correct guesses as
    won."""

    with op.batch_alter_table("user_frame_score", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("rounds_seen", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("rounds_won", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade():
    """Drop the win-rate columns."""

    with op.batch_alter_table("user_frame_score", schema=None) as batch_op:
        batch_op.drop_column("rounds_won")
        batch_op.drop_column("rounds_seen")
