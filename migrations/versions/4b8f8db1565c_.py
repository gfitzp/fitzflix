"""empty message

Revision ID: 4b8f8db1565c
Revises: 40b018e83e16
Create Date: 2024-10-23 21:03:29.394165

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '4b8f8db1565c'
down_revision = '40b018e83e16'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('movie', sa.Column('criterion_quality_id', sa.Integer(), nullable=True))
    op.drop_constraint('movie_ibfk_1', 'movie', type_='foreignkey')
    op.create_foreign_key(None, 'movie', 'ref_quality', ['criterion_quality_id'], ['id'])
    op.drop_column('movie', 'criterion_quality')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('movie', sa.Column('criterion_quality', mysql.INTEGER(display_width=11), autoincrement=False, nullable=True))
    op.drop_constraint(None, 'movie', type_='foreignkey')
    op.create_foreign_key('movie_ibfk_1', 'movie', 'ref_quality', ['criterion_quality'], ['id'])
    op.drop_column('movie', 'criterion_quality_id')
    # ### end Alembic commands ###
