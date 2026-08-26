"""Per-user watchlist availability-alert opt-ins (#156/#230)

Revision ID: c8e15a2d4f37
Revises: b7d20c4f1a86
Create Date: 2026-08-26 18:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c8e15a2d4f37"
down_revision = "b7d20c4f1a86"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "notify_availability",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "notify_rentals", sa.Boolean(), nullable=False, server_default="0"
            )
        )


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("notify_rentals")
        batch_op.drop_column("notify_availability")
