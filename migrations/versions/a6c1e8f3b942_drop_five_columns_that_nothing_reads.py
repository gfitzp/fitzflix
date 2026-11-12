"""Drop 5 columns that nothing reads (#724)

The orphan search of scan 31 found these columns. No code reads them,
and the step loses no information:

- file.aws_date_deleted: no code writes it since March 2024, and each
  row is NULL. 7 queries had a filter on it that was always true.
- file.crop: never written, each row NULL.
- user_movie_review.whole_stars and half_stars: written from the rating
  and never read. The downgrade computes them again from
  modified_rating.
- app_setting.updated_by: written and never read. The log names the
  admin who changed a setting. The table had no row at the time of the
  step.

The step changes no other column and no row. Run it with the processes
stopped, before the start of the new code. The old code names the
columns in its queries.

Revision ID: a6c1e8f3b942
Revises: e5b8d2a4c917
Create Date: 2026-11-12 14:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a6c1e8f3b942"
down_revision = "e5b8d2a4c917"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("file") as batch:
        batch.drop_column("aws_date_deleted")
        batch.drop_column("crop")
    with op.batch_alter_table("user_movie_review") as batch:
        batch.drop_column("whole_stars")
        batch.drop_column("half_stars")
    with op.batch_alter_table("app_setting") as batch:
        batch.drop_column("updated_by")


def downgrade():
    with op.batch_alter_table("app_setting") as batch:
        batch.add_column(sa.Column("updated_by", sa.String(length=120), nullable=True))
    with op.batch_alter_table("user_movie_review") as batch:
        batch.add_column(sa.Column("whole_stars", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("half_stars", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE user_movie_review "
        "SET whole_stars = FLOOR(modified_rating), "
        "half_stars = CASE WHEN modified_rating = FLOOR(modified_rating) "
        "THEN 0 ELSE 1 END "
        "WHERE modified_rating IS NOT NULL"
    )
    with op.batch_alter_table("file") as batch:
        batch.add_column(sa.Column("crop", sa.String(length=19), nullable=True))
        batch.add_column(sa.Column("aws_date_deleted", sa.DateTime(), nullable=True))
