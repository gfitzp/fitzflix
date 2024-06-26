"""empty message

Revision ID: 92a4ac4470a8
Revises: d568c66a57fa
Create Date: 2024-04-02 22:13:13.668014

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '92a4ac4470a8'
down_revision = 'd568c66a57fa'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('file', sa.Column('custom_poster', sa.Boolean(), nullable=True))
    op.drop_column('file', 'custom_poster_path')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('file', sa.Column('custom_poster_path', mysql.VARCHAR(length=64), nullable=True))
    op.drop_column('file', 'custom_poster')
    # ### end Alembic commands ###
