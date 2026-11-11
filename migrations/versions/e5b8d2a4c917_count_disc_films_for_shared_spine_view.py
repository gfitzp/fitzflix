"""Count only the films on disc for a shared spine in the criterion_collection view (#696)

The criterion_collection reporting view has the rule of the main
Criterion collection of the Plex sync (app/plex_collections.py). A film
stays out of that collection when another film shares its spine number.
The view counted each movie record for that test, also a catalog record
with no files. The sync now counts only the movies with a main feature
on physical media, because only such a movie can be in a box-set
collection. This step gives the view the same rule.

The step changes a view only. It changes no table and no row. The app
does not read the view. Run it before or after the restart.

Revision ID: e5b8d2a4c917
Revises: d9f2b5c7e416
Create Date: 2026-11-10 23:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e5b8d2a4c917"
down_revision = "d9f2b5c7e416"
branch_labels = None
depends_on = None


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
                        AND movie.id IN (
                                SELECT disc.movie_id
                                  FROM file disc
                                  JOIN ref_quality dq ON dq.id = disc.quality_id
                                 WHERE disc.movie_id IS NOT NULL
                                   AND disc.feature_type_id IS NULL
                                   AND dq.physical_media = 1
                            )
                      GROUP BY movie.criterion_spine_number
                     HAVING COUNT(movie.id) > 1
                 )
      ) versions
     WHERE version_rank = 1
     ORDER BY criterion_spine_number
"""

# The definition of migration c58102447ee3, for the downgrade.

OLD_CRITERION_COLLECTION = """
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


def upgrade():
    op.execute(CRITERION_COLLECTION)


def downgrade():
    op.execute(OLD_CRITERION_COLLECTION)
