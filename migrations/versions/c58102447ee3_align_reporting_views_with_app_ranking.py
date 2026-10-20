"""Align the reporting views with the ranking of the app (#273)

ranked_files now uses the rules of movie_file_rank() and tv_file_rank() in
app/models.py. A movie group is the movie id, the feature type, the Plex
title, and the edition. Full screen copies rank below the others. A tie
goes to the lowest file id, as in the app. ROW_NUMBER() gives 1 winner
for each group.

criterion_collection now uses the rule of the Plex collection sync
(app/plex_collections.py, #281 and #284). It gives 1 version of each
film. The version with no edition has priority. Else the first edition
by name. Each row is 1 file, with its edition and its path.

Revision ID: c58102447ee3
Revises: b7c3e91d4a20
Create Date: 2026-10-20 10:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c58102447ee3"
down_revision = "b7c3e91d4a20"
branch_labels = None
depends_on = None


RANKED_FILES = """
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

# The version order compares the edition names as bytes. That is the
# order of the Python min() in app.plex_collections._one_version.

CRITERION_COLLECTION = """
    CREATE OR REPLACE VIEW criterion_collection AS
    SELECT title, year, criterion_spine_number, criterion_set_title,
           criterion_disc_owned, quality_title, edition, file_path
      FROM (
          SELECT m.title AS title,
                 m.year AS year,
                 m.criterion_spine_number AS criterion_spine_number,
                 m.criterion_set_title AS criterion_set_title,
                 m.criterion_disc_owned AS criterion_disc_owned,
                 q.quality_title AS quality_title,
                 f.edition AS edition,
                 f.file_path AS file_path,
                 DENSE_RANK() OVER (
                     PARTITION BY m.id
                     ORDER BY CAST(COALESCE(f.edition, '') AS BINARY)
                 ) AS version_rank
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
      ) versions
     WHERE version_rank = 1
     ORDER BY criterion_spine_number
"""

# The definitions of migration 4a34ba4d069f, for the downgrade.

OLD_RANKED_FILES = """
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

OLD_CRITERION_COLLECTION = """
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


def upgrade():
    op.execute(RANKED_FILES)
    op.execute(CRITERION_COLLECTION)


def downgrade():
    op.execute(OLD_RANKED_FILES)
    op.execute(OLD_CRITERION_COLLECTION)
