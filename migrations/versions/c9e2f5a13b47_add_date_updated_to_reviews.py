"""Add date_updated to user_movie_review

Revision ID: c9e2f5a13b47
Revises: b7c4d1e8a920
Create Date: 2026-08-08 21:50:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c9e2f5a13b47"
down_revision = "b7c4d1e8a920"
branch_labels = None
depends_on = None


def upgrade():
    # Editing a review keeps date_reviewed as the original review date;
    # date_updated records when the text was last changed

    op.add_column(
        "user_movie_review", sa.Column("date_updated", sa.DateTime(), nullable=True)
    )


def downgrade():
    op.drop_column("user_movie_review", "date_updated")
