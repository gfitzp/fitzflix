"""TV episodes (#78): per-slot TMDb episode metadata in its own table.

Revision ID: 56eb7f483f58
Revises: e8a41f5c9d27
Create Date: 2026-08-21 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "56eb7f483f58"
down_revision = "e8a41f5c9d27"
branch_labels = None
depends_on = None


def upgrade():
    """Create the tv_episode table: one row per TMDb-known
    series/season/episode slot, rows cascading away with their series.

    The unique constraint doubles as the lookup index — its leftmost
    prefixes serve the by-series and by-season queries — so no separate
    series_id index is created.
    """

    op.create_table(
        "tv_episode",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("tmdb_episode_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("air_date", sa.DateTime(), nullable=True),
        sa.Column("runtime", sa.Integer(), nullable=True),
        sa.Column("tmdb_still_path", sa.String(length=64), nullable=True),
        sa.Column("tmdb_data_as_of", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["series_id"], ["tv_series.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "season", "episode"),
    )


def downgrade():
    """Drop the tv_episode table."""

    op.drop_table("tv_episode")
