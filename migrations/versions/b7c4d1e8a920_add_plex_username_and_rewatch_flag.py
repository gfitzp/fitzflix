"""Add plex_username to user and rewatch flag to reviews

Revision ID: b7c4d1e8a920
Revises: a3cb79e50650
Create Date: 2026-08-08 21:05:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b7c4d1e8a920"
down_revision = "a3cb79e50650"
branch_labels = None
depends_on = None


def upgrade():
    # plex_username maps Plex watchers to Fitzflix users for personal watch
    # attribution; rewatch marks repeat viewings (NULL = unknown, for rows
    # that predate the flag)

    op.add_column(
        "user", sa.Column("plex_username", sa.String(length=64), nullable=True)
    )
    op.create_unique_constraint("uq_user_plex_username", "user", ["plex_username"])
    op.add_column(
        "user_movie_review", sa.Column("rewatch", sa.Boolean(), nullable=True)
    )


def downgrade():
    op.drop_column("user_movie_review", "rewatch")
    op.drop_constraint("uq_user_plex_username", "user", type_="unique")
    op.drop_column("user", "plex_username")
