"""empty message

Revision ID: d5f58bdea78b
Revises: e62eda6d27de
Create Date: 2024-04-02 18:42:24.489589

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'd5f58bdea78b'
down_revision = 'e62eda6d27de'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('file', sa.Column('edition', sa.String(length=219), nullable=True))
    op.drop_index('ix_file_version', table_name='file')
    op.create_index(op.f('ix_file_edition'), 'file', ['edition'], unique=False)
    op.drop_column('file', 'version')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('file', sa.Column('version', mysql.VARCHAR(length=219), nullable=True))
    op.drop_index(op.f('ix_file_edition'), table_name='file')
    op.create_index('ix_file_version', 'file', ['version'], unique=False)
    op.drop_column('file', 'edition')
    # ### end Alembic commands ###
