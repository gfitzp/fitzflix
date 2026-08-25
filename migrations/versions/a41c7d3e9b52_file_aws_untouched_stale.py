"""Flag a file whose S3 archive no longer matches its local copy

Revision ID: a41c7d3e9b52
Revises: 7f98f5ebcc25
Create Date: 2026-08-25 12:25:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a41c7d3e9b52"
down_revision = "7f98f5ebcc25"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("file", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "aws_untouched_stale",
                sa.Boolean(),
                server_default="0",
                nullable=False,
            )
        )


def downgrade():
    with op.batch_alter_table("file", schema=None) as batch_op:
        batch_op.drop_column("aws_untouched_stale")
