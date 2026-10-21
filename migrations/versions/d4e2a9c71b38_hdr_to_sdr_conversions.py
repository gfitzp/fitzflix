"""HDR to SDR conversions, archive-only originals, held imports (#292)

The file table gets the color tags of the video track and the 2
conversion fields. A conversion records the quality and the color of
its original. archived_original keeps an original in S3 with no library
file. held_import is a conversion candidate that waits for an admin
decision.

ranked_files now has the conversion rule of movie_file_rank() and
tv_file_rank(). At the same quality, a real rip ranks above a
conversion.

Revision ID: d4e2a9c71b38
Revises: c58102447ee3
Create Date: 2026-10-20 23:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d4e2a9c71b38"
down_revision = "c58102447ee3"
branch_labels = None
depends_on = None


RANKED_FILES = """
    CREATE OR REPLACE VIEW ranked_files AS
    SELECT file.id AS id,
           file.untouched_basename AS untouched_basename,
           ROW_NUMBER() OVER (
               PARTITION BY movie.id, file.feature_type_id,
                            file.plex_title, file.edition
               ORDER BY file.fullscreen ASC, q.preference DESC,
                        file.converted_from_quality IS NOT NULL ASC,
                        file.id ASC
           ) AS `rank`
      FROM file
      JOIN movie ON movie.id = file.movie_id
      JOIN ref_quality q ON q.id = file.quality_id
    UNION ALL
    SELECT file.id AS id,
           file.untouched_basename AS untouched_basename,
           ROW_NUMBER() OVER (
               PARTITION BY tv_series.id, file.season, file.episode
               ORDER BY file.fullscreen ASC, q.preference DESC,
                        file.last_episode DESC,
                        file.converted_from_quality IS NOT NULL ASC,
                        file.id ASC
           ) AS `rank`
      FROM file
      JOIN tv_series ON tv_series.id = file.series_id
      JOIN ref_quality q ON q.id = file.quality_id
"""

# The definition of migration c58102447ee3, for the downgrade.

OLD_RANKED_FILES = """
    CREATE OR REPLACE VIEW ranked_files AS
    SELECT file.id AS id,
           file.untouched_basename AS untouched_basename,
           ROW_NUMBER() OVER (
               PARTITION BY movie.id, file.feature_type_id,
                            file.plex_title, file.edition
               ORDER BY file.fullscreen ASC, q.preference DESC, file.id ASC
           ) AS `rank`
      FROM file
      JOIN movie ON movie.id = file.movie_id
      JOIN ref_quality q ON q.id = file.quality_id
    UNION ALL
    SELECT file.id AS id,
           file.untouched_basename AS untouched_basename,
           ROW_NUMBER() OVER (
               PARTITION BY tv_series.id, file.season, file.episode
               ORDER BY file.fullscreen ASC, q.preference DESC,
                        file.last_episode DESC, file.id ASC
           ) AS `rank`
      FROM file
      JOIN tv_series ON tv_series.id = file.series_id
      JOIN ref_quality q ON q.id = file.quality_id
"""


def upgrade():
    with op.batch_alter_table("file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("color_primaries", sa.String(length=64)))
        batch_op.add_column(
            sa.Column("transfer_characteristics", sa.String(length=64))
        )
        batch_op.add_column(sa.Column("converted_from_quality", sa.String(length=32)))
        batch_op.add_column(sa.Column("converted_from_format", sa.String(length=255)))

    op.create_table(
        "archived_original",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_library", sa.String(length=32), nullable=False),
        sa.Column("movie_id", sa.Integer(), nullable=True),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("season", sa.Integer(), nullable=True),
        sa.Column("episode", sa.Integer(), nullable=True),
        sa.Column("plex_title", sa.String(length=255), nullable=True),
        sa.Column("edition", sa.String(length=219), nullable=True),
        sa.Column("quality_title", sa.String(length=32), nullable=False),
        sa.Column("hdr_format", sa.String(length=255), nullable=True),
        sa.Column("untouched_basename", sa.String(length=255), nullable=False),
        sa.Column("aws_untouched_key", sa.String(length=255), nullable=False),
        sa.Column("aws_untouched_filesize_bytes", sa.BigInteger(), nullable=True),
        sa.Column("aws_untouched_date_uploaded", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("date_added", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["movie_id"], ["movie.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["series_id"], ["tv_series.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("aws_untouched_key"),
    )
    with op.batch_alter_table("archived_original", schema=None) as batch_op:
        batch_op.create_index("ix_archived_original_movie_id", ["movie_id"])
        batch_op.create_index("ix_archived_original_series_id", ["series_id"])

    op.create_table(
        "held_import",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("held_path", sa.String(length=512), nullable=False),
        sa.Column("untouched_basename", sa.String(length=255), nullable=False),
        sa.Column("file_details", sa.Text(), nullable=False),
        sa.Column("source_format", sa.String(length=255), nullable=True),
        sa.Column("filesize_bytes", sa.BigInteger(), nullable=True),
        sa.Column("existing_file_id", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("date_held", sa.DateTime(), nullable=False),
        sa.Column("date_decided", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["existing_file_id"], ["file.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("held_path"),
    )

    op.execute(RANKED_FILES)


def downgrade():
    op.execute(OLD_RANKED_FILES)
    op.drop_table("held_import")
    with op.batch_alter_table("archived_original", schema=None) as batch_op:
        batch_op.drop_index("ix_archived_original_series_id")
        batch_op.drop_index("ix_archived_original_movie_id")
    op.drop_table("archived_original")
    with op.batch_alter_table("file", schema=None) as batch_op:
        batch_op.drop_column("converted_from_format")
        batch_op.drop_column("converted_from_quality")
        batch_op.drop_column("transfer_characteristics")
        batch_op.drop_column("color_primaries")
