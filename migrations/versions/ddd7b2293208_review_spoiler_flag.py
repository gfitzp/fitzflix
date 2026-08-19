"""review spoiler flag

Letterboxd renders its spoiler checkbox into the feed as an injected
"This review may contain spoilers." paragraph; the sync now strips
that from review text and stores the flag here instead. Nullable:
only feed-synced rows know it (the CSV export has no spoiler column).

Revision ID: ddd7b2293208
Revises: d25b71aea079
Create Date: 2026-08-18 23:35:33.428672

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ddd7b2293208'
down_revision = 'd25b71aea079'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_movie_review", sa.Column("contains_spoilers", sa.Boolean(), nullable=True)
    )


def downgrade():
    op.drop_column("user_movie_review", "contains_spoilers")
