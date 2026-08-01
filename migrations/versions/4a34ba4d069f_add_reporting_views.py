"""Add reporting views

These views aren't used by the application itself; they exist for ad-hoc SQL
queries against the library. They were originally created by hand, so this
migration brings them under version control. CREATE OR REPLACE makes it safe
to run against a database where they already exist; the definer is left to
default to the migrating user instead of the original hand-created
root@localhost definer.

ranked_files ranks each file within its movie or episode group by quality
preference; criterion_collection lists the best owned copy of each uniquely-
numbered Criterion Collection spine.

Revision ID: 4a34ba4d069f
Revises: 71f170936781
Create Date: 2026-08-01 12:20:49.170096

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '4a34ba4d069f'
down_revision = '71f170936781'
branch_labels = None
depends_on = None


def upgrade():
    # criterion_collection selects from ranked_files, so create that first

    op.execute(
        """
        CREATE OR REPLACE VIEW ranked_files AS
        SELECT file.id AS id,
               file.untouched_basename AS untouched_basename,
               DENSE_RANK() OVER (
                   PARTITION BY movie.title, movie.year,
                                file.feature_type_id, file.plex_title
                   ORDER BY q.preference DESC
               ) AS `rank`
          FROM file
          JOIN movie ON movie.id = file.movie_id
          JOIN ref_quality q ON q.id = file.quality_id
        UNION
        SELECT file.id AS id,
               file.untouched_basename AS untouched_basename,
               DENSE_RANK() OVER (
                   PARTITION BY tv_series.id, file.season, file.episode
                   ORDER BY q.preference DESC, file.last_episode DESC
               ) AS `rank`
          FROM file
          JOIN tv_series ON tv_series.id = file.series_id
          JOIN ref_quality q ON q.id = file.quality_id
        """
    )

    op.execute(
        """
        CREATE OR REPLACE VIEW criterion_collection AS
        SELECT DISTINCT m.title AS title,
               m.year AS year,
               m.criterion_spine_number AS criterion_spine_number,
               m.criterion_set_title AS criterion_set_title,
               m.criterion_disc_owned AS criterion_disc_owned,
               q.quality_title AS quality_title
          FROM movie m
          JOIN file f ON f.movie_id = m.id
          JOIN ranked_files rf ON rf.id = f.id
          JOIN ref_quality q ON q.id = f.quality_id
         WHERE rf.`rank` = 1
           AND f.feature_type_id IS NULL
           AND m.criterion_spine_number IS NOT NULL
           AND m.custom_poster IS NOT NULL
           AND q.physical_media = 1
           AND m.criterion_spine_number NOT IN (
                   SELECT movie.criterion_spine_number
                     FROM movie
                    WHERE movie.criterion_spine_number IS NOT NULL
                    GROUP BY movie.criterion_spine_number
                   HAVING COUNT(movie.id) > 1
               )
         ORDER BY m.criterion_spine_number
        """
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS criterion_collection")
    op.execute("DROP VIEW IF EXISTS ranked_files")
