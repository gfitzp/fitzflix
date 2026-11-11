"""Clear the Unknown tier from the Criterion release format of each film (#685)

The "Released as" list of the Criterion form stored the Unknown tier
for a film with no recorded format. That tier has preference 1. Thus,
the film was settled as soon as the disc was owned. The list now stores
no value for "Unknown". This step clears the Unknown tier from the rows
that hold it. The new code works without this step. Run it before or
after the restart.

The downgrade does not change the data. The old code reads no value as
"Unknown" too. A row that holds no value is correct for both versions.
The old values cannot come back, and no reader needs them.

Revision ID: d9f2b5c7e416
Revises: c8e4f1a2d305
Create Date: 2026-11-10 19:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d9f2b5c7e416"
down_revision = "c8e4f1a2d305"
branch_labels = None
depends_on = None


MOVIE = sa.table("movie", sa.column("id"), sa.column("criterion_quality_id"))
REF_QUALITY = sa.table("ref_quality", sa.column("id"), sa.column("quality_title"))


def clear_unknown_criterion_quality(connection):
    """Set the Criterion release format to no value where it is the Unknown tier."""

    unknown_id = connection.execute(
        sa.select(REF_QUALITY.c.id).where(REF_QUALITY.c.quality_title == "Unknown")
    ).scalar()
    if unknown_id is None:
        return
    connection.execute(
        MOVIE.update()
        .where(MOVIE.c.criterion_quality_id == unknown_id)
        .values(criterion_quality_id=None)
    )


def upgrade():
    clear_unknown_criterion_quality(op.get_bind())


def downgrade():
    pass
