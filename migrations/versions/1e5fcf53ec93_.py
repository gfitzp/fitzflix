"""empty message

Revision ID: 1e5fcf53ec93
Revises: 92a4ac4470a8
Create Date: 2024-04-02 22:26:24.234377

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '1e5fcf53ec93'
down_revision = '92a4ac4470a8'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('file', 'custom_poster',
               existing_type=mysql.TINYINT(display_width=1),
               type_=sa.String(length=64),
               existing_nullable=True)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('file', 'custom_poster',
               existing_type=sa.String(length=64),
               type_=mysql.TINYINT(display_width=1),
               existing_nullable=True)
    # ### end Alembic commands ###