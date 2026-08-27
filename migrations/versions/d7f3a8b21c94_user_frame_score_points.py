"""Extra Difficult standings (#202): the running points total.

Revision ID: d7f3a8b21c94
Revises: 5c3032dcb331
Create Date: 2026-08-26 20:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d7f3a8b21c94"
down_revision = "5c3032dcb331"
branch_labels = None
depends_on = None


def upgrade():
    """Add user_frame_score.points — 3/2/1 per round by how early in
    the zoom-out the correct guess landed."""

    with op.batch_alter_table("user_frame_score", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("points", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade():
    """Drop the points column."""

    with op.batch_alter_table("user_frame_score", schema=None) as batch_op:
        batch_op.drop_column("points")
