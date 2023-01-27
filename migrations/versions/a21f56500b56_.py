"""empty message

Revision ID: a21f56500b56
Revises: 76dac814b569
Create Date: 2023-01-25 15:36:27.202202

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a21f56500b56"
down_revision = "76dac814b569"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        "file_audio_track", sa.Column("streamorder", sa.Integer(), nullable=False)
    )
    op.add_column(
        "file_subtitle_track", sa.Column("streamorder", sa.Integer(), nullable=False)
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("file_subtitle_track", "streamorder")
    op.drop_column("file_audio_track", "streamorder")
    # ### end Alembic commands ###
