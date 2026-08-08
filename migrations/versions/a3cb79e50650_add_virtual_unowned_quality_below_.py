"""Add virtual Not-in-library quality below Unknown

Revision ID: a3cb79e50650
Revises: fee519875a05
Create Date: 2026-08-08 13:33:58.333287

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'a3cb79e50650'
down_revision = 'fee519875a05'
branch_labels = None
depends_on = None


def upgrade():
    # A virtual quality representing films with no files at all, so the
    # shopping list's quality range can include or exclude unowned films.
    # Preferences shift up by one to make room at the bottom; ids are left
    # untouched (they're foreign-keyed and baked into URLs), and no file is
    # ever assigned this quality.

    op.execute("UPDATE ref_quality SET preference = preference + 1")
    op.execute(
        "INSERT INTO ref_quality (quality_title, preference, physical_media) "
        "VALUES ('Not in library', 0, 0)"
    )


def downgrade():
    op.execute("DELETE FROM ref_quality WHERE quality_title = 'Not in library'")
    op.execute("UPDATE ref_quality SET preference = preference - 1")
