"""Drop the season tables and the date of the best score (#724)

No code reads these. The owner chose to drop them on 2026-11-12.

- tmdb_season and its link table tv_seasons: each TV refresh wrote the
  season list of TMDB (1,377 seasons and 1,405 links at the time of the
  step). No page and no job read the rows. TMDB can give the list
  again. The series record keeps its season count and its episode
  count.
- user_frame_score.date_best: the time of the best streak of a player
  in the frame game. The game wrote it and never showed it. 1 row had a
  value. That value is lost.

The downgrade makes the 2 tables again with no rows, and the column
with no value. The code of the earlier revision fills the tables at
the next TV refresh.

Run the step with the processes stopped, before the start of the new
code. The old code names the tables in its queries.

Revision ID: b8d4f2a7c315
Revises: a6c1e8f3b942
Create Date: 2026-11-12 15:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b8d4f2a7c315"
down_revision = "a6c1e8f3b942"
branch_labels = None
depends_on = None


def upgrade():
    # The link table has the foreign keys. It goes first.
    op.drop_table("tv_seasons")
    op.drop_table("tmdb_season")
    with op.batch_alter_table("user_frame_score") as batch:
        batch.drop_column("date_best")


def downgrade():
    with op.batch_alter_table("user_frame_score") as batch:
        batch.add_column(sa.Column("date_best", sa.DateTime(), nullable=True))
    op.create_table(
        "tmdb_season",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("air_date", sa.DateTime(), nullable=True),
        sa.Column("episode_count", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("tmdb_poster_path", sa.String(length=64), nullable=True),
        sa.Column("season_number", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "tv_seasons",
        sa.Column("tv_id", sa.Integer(), nullable=True),
        sa.Column("season_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["season_id"], ["tmdb_season.id"]),
        sa.ForeignKeyConstraint(["tv_id"], ["tv_series.id"]),
    )
