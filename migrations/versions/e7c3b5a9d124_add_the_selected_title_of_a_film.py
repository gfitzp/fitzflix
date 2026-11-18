"""Add the selected title of a film

An admin can select the title of a film from the titles that TMDB lists
for it. The new column holds that title. It has no value for a film
that follows the main title of TMDB, and each film starts with no
value.

The downgrade drops the column. The films keep their titles. A later
refresh then gives each film the main title of TMDB again.

Revision ID: e7c3b5a9d124
Revises: d4a91c6e7b20
Create Date: 2026-11-18 15:40:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e7c3b5a9d124"
down_revision = "d4a91c6e7b20"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "movie", sa.Column("chosen_title", sa.String(length=220), nullable=True)
    )


def downgrade():
    op.drop_column("movie", "chosen_title")
