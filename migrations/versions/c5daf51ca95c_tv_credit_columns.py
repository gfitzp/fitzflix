"""TV credit columns (#78): billing order and episode counts from
TMDb's aggregate credits.

Revision ID: c5daf51ca95c
Revises: 56eb7f483f58
Create Date: 2026-08-21 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c5daf51ca95c"
down_revision = "56eb7f483f58"
branch_labels = None
depends_on = None


def upgrade():
    """Add billing_order + episode_count to tv_cast and episode_count to
    tv_crew — the aggregate-credits fields the series page sorts and
    labels by."""

    op.add_column("tv_cast", sa.Column("billing_order", sa.Integer(), nullable=True))
    op.add_column("tv_cast", sa.Column("episode_count", sa.Integer(), nullable=True))
    op.add_column("tv_crew", sa.Column("episode_count", sa.Integer(), nullable=True))


def downgrade():
    """Drop the aggregate-credits columns."""

    op.drop_column("tv_crew", "episode_count")
    op.drop_column("tv_cast", "episode_count")
    op.drop_column("tv_cast", "billing_order")
