"""Drop tv_episode and TVSeries.episode_source: no stored episode data

Episode metadata is no longer stored from any source — TMDB feeds
series-level information only, and the Sonarr/TVDB episode pipeline
(#162) was reverted over data-provenance concerns.

Revision ID: e5b7c92a41d8
Revises: b8e2f4a61d07
Create Date: 2026-08-31 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5b7c92a41d8'
down_revision = 'b8e2f4a61d07'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('tv_episode')
    with op.batch_alter_table('tv_series', schema=None) as batch_op:
        batch_op.drop_column('episode_source')


def downgrade():
    with op.batch_alter_table('tv_series', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'episode_source',
                sa.String(length=16),
                nullable=False,
                server_default='tmdb',
            )
        )
    op.create_table(
        'tv_episode',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('series_id', sa.Integer(), nullable=False),
        sa.Column('season', sa.Integer(), nullable=False),
        sa.Column('episode', sa.Integer(), nullable=False),
        sa.Column('tmdb_episode_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=256), nullable=True),
        sa.Column('overview', sa.Text(), nullable=True),
        sa.Column('air_date', sa.DateTime(), nullable=True),
        sa.Column('runtime', sa.Integer(), nullable=True),
        sa.Column('tmdb_still_path', sa.String(length=64), nullable=True),
        sa.Column('tmdb_data_as_of', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['series_id'], ['tv_series.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('series_id', 'season', 'episode'),
    )
