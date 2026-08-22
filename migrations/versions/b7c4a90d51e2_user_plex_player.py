"""Per-user Plex playback devices: each user's play buttons target
their own player, set from the Profile page.

Revision ID: b7c4a90d51e2
Revises: c5daf51ca95c
Create Date: 2026-08-21 20:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b7c4a90d51e2"
down_revision = "c5daf51ca95c"
branch_labels = None
depends_on = None


def upgrade():
    """Add the user's playback device: Companion address + machine id."""

    op.add_column("user", sa.Column("plex_player_address", sa.String(64)))
    op.add_column("user", sa.Column("plex_player_id", sa.String(64)))


def downgrade():
    """Drop the playback device columns."""

    op.drop_column("user", "plex_player_id")
    op.drop_column("user", "plex_player_address")
