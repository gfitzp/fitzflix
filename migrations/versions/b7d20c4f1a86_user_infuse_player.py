"""Per-user Infuse playback target and default-player choice (#192)

Revision ID: b7d20c4f1a86
Revises: a41c7d3e9b52
Create Date: 2026-08-26 14:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "b7d20c4f1a86"
down_revision = "a41c7d3e9b52"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("infuse_player_address", sa.String(64)))
        batch_op.add_column(sa.Column("infuse_player_credentials", sa.String(512)))
        batch_op.add_column(sa.Column("default_player", sa.String(8)))


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("default_player")
        batch_op.drop_column("infuse_player_credentials")
        batch_op.drop_column("infuse_player_address")
