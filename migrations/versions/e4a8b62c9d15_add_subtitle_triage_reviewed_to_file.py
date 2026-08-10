"""Add subtitle_triage_reviewed to file

Revision ID: e4a8b62c9d15
Revises: c9e2f5a13b47
Create Date: 2026-08-10 17:05:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e4a8b62c9d15"
down_revision = "c9e2f5a13b47"
branch_labels = None
depends_on = None


def upgrade():
    # Subtitle-triage dismissals live on the file: track rows are deleted
    # and rebuilt by metadata rescans, so a per-track flag wouldn't survive

    op.add_column(
        "file", sa.Column("subtitle_triage_reviewed", sa.DateTime(), nullable=True)
    )


def downgrade():
    op.drop_column("file", "subtitle_triage_reviewed")
