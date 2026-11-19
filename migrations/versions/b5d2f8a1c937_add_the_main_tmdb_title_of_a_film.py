"""Add the main TMDB title of a film

The column tmdb_title holds the title that the pages show. For a film
with a selected title, that is the selected title. The new column holds
the main title of TMDB for each film. Thus, a search finds a film with
a selected title by its main title too.

Each film with no selected title gets the value of tmdb_title, because
that is its main title. A film with a selected title gets its value
from the next refresh.

The downgrade drops the column. The films keep their titles.

Revision ID: b5d2f8a1c937
Revises: e7c3b5a9d124
Create Date: 2026-11-19 09:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b5d2f8a1c937"
down_revision = "e7c3b5a9d124"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "movie", sa.Column("tmdb_main_title", sa.String(length=256), nullable=True)
    )
    op.execute(
        "UPDATE movie SET tmdb_main_title = tmdb_title WHERE chosen_title IS NULL"
    )


def downgrade():
    op.drop_column("movie", "tmdb_main_title")
