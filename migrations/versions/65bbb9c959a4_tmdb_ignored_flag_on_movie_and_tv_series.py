"""tmdb_ignored flag on movie and tv_series

Revision ID: 65bbb9c959a4
Revises: b7c4a90d51e2
Create Date: 2026-08-24 12:48:22.683915

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "65bbb9c959a4"
down_revision = "b7c4a90d51e2"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("movie", "tv_series"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "tmdb_ignored",
                    sa.Boolean(),
                    nullable=False,
                    server_default="0",
                )
            )


def downgrade():
    for table in ("movie", "tv_series"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("tmdb_ignored")
