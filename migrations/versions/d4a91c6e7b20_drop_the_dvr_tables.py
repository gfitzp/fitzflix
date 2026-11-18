"""Drop the tables of the virtual DVR

The virtual DVR is not in the code. The owner chose to drop its tables
on 2026-11-18.

- dvr_channel: the definition of each channel. It had 16 rows at the
  time of the step. These were the channels that the first build made.
  No person changed them.
- dvr_channel_movies and dvr_channel_series: the titles that a person
  put on a channel. The 2 tables had no rows.

The downgrade makes the 3 tables again with no rows.

Revision ID: d4a91c6e7b20
Revises: c2e7a9d4f618
Create Date: 2026-11-18 15:10:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d4a91c6e7b20"
down_revision = "c2e7a9d4f618"
branch_labels = None
depends_on = None


def upgrade():
    # The link tables have the foreign keys. They go first.
    op.drop_table("dvr_channel_movies")
    op.drop_table("dvr_channel_series")
    op.drop_table("dvr_channel")


def downgrade():
    op.create_table(
        "dvr_channel",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("include_movies", sa.Boolean(), nullable=False),
        sa.Column("include_tv", sa.Boolean(), nullable=False),
        sa.Column("genres", sa.Text(), nullable=True),
        sa.Column("keywords", sa.Text(), nullable=True),
        sa.Column("network_country", sa.String(length=16), nullable=True),
        sa.Column("title_pins", sa.Text(), nullable=True),
        sa.Column("criterion_only", sa.Boolean(), nullable=False),
        sa.Column("leaving_only", sa.Boolean(), nullable=False),
        sa.Column("date_created", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("number"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "dvr_channel_series",
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["channel_id"], ["dvr_channel.id"]),
        sa.ForeignKeyConstraint(["series_id"], ["tv_series.id"]),
    )
    op.create_table(
        "dvr_channel_movies",
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("movie_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["channel_id"], ["dvr_channel.id"]),
        sa.ForeignKeyConstraint(["movie_id"], ["movie.id"]),
    )
