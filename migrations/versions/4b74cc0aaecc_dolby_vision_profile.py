"""dolby vision profile

The Dolby Vision flavor ("5", "7", "8.1", …) parsed from MediaInfo's
HDR-format string during track scanning (#65). NULL for non-DV files
and for files not rescanned since the field shipped.

Revision ID: 4b74cc0aaecc
Revises: ddd7b2293208
Create Date: 2026-08-19 00:12:15.285485

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4b74cc0aaecc'
down_revision = 'ddd7b2293208'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "file", sa.Column("dolby_vision_profile", sa.String(length=8), nullable=True)
    )


def downgrade():
    op.drop_column("file", "dolby_vision_profile")
