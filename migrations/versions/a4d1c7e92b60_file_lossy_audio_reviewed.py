"""File.lossy_audio_reviewed for the lossy-audio triage (#212)

Revision ID: a4d1c7e92b60
Revises: e9c47b31d5a8
Create Date: 2026-08-30 15:50:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a4d1c7e92b60'
down_revision = 'e9c47b31d5a8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lossy_audio_reviewed', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.drop_column('lossy_audio_reviewed')
