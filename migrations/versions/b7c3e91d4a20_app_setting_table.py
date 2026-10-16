"""app_setting table for the Admin Settings page

Revision ID: b7c3e91d4a20
Revises: 0d816daf3ec3
Create Date: 2026-10-16 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7c3e91d4a20'
down_revision = '0d816daf3ec3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('app_setting',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_by', sa.String(length=120), nullable=True),
    sa.Column('date_updated', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('name')
    )


def downgrade():
    op.drop_table('app_setting')
