"""letterboxd sync fields

Revision ID: d25b71aea079
Revises: 3a26bd38a0d1
Create Date: 2026-08-18 22:58:23

The RSS sync (#61): each user names their Letterboxd account, and every
diary row ingested from (or matched by) the feed carries its item guid —
the dedup and edit-matching key, and the guard that keeps feed-created
rows out of the CSV export.
"""

from alembic import op
import sqlalchemy as sa

revision = "d25b71aea079"
down_revision = "3a26bd38a0d1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user", sa.Column("letterboxd_username", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "user_movie_review",
        sa.Column("letterboxd_guid", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_user_movie_review_letterboxd_guid",
        "user_movie_review",
        ["letterboxd_guid"],
        unique=True,
    )


def downgrade():
    op.drop_index(
        "ix_user_movie_review_letterboxd_guid", table_name="user_movie_review"
    )
    op.drop_column("user_movie_review", "letterboxd_guid")
    op.drop_column("user", "letterboxd_username")
