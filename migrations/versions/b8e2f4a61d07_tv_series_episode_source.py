"""TVSeries.episode_source: which service owns tv_episode rows (#162)

Revision ID: b8e2f4a61d07
Revises: a4d1c7e92b60
Create Date: 2026-08-31 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8e2f4a61d07'
down_revision = 'a4d1c7e92b60'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tv_series', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'episode_source',
                sa.String(length=16),
                nullable=False,
                server_default='tmdb',
            )
        )


def downgrade():
    with op.batch_alter_table('tv_series', schema=None) as batch_op:
        batch_op.drop_column('episode_source')
