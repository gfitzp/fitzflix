"""Drop the derived filesize columns; bytes is the stored fact

Revision ID: 7f98f5ebcc25
Revises: 65bbb9c959a4
Create Date: 2026-08-24 18:30:27.484192

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '7f98f5ebcc25'
down_revision = '65bbb9c959a4'
branch_labels = None
depends_on = None


def upgrade():
    """Drop the two derived size columns.

    Nothing read them but one line of one template, which now formats
    from filesize_bytes the way the derived-copy row beside it always
    has. No data is lost: both were rounded restatements of a column
    that stays.
    """

    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.drop_column('filesize_gigabytes')
        batch_op.drop_column('filesize_megabytes')


def downgrade():
    """Restore the columns AND their values.

    Re-adding them empty would be a silent downgrade — the template this
    migration exists to simplify would then print blanks. They are pure
    functions of filesize_bytes, so recompute rather than leave NULL.
    """

    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.add_column(sa.Column('filesize_megabytes', mysql.DECIMAL(precision=8, scale=1), nullable=True))
        batch_op.add_column(sa.Column('filesize_gigabytes', mysql.DECIMAL(precision=5, scale=1), nullable=True))

    op.execute(
        "UPDATE file SET "
        "filesize_megabytes = ROUND(filesize_bytes / 1048576, 1), "
        "filesize_gigabytes = ROUND(filesize_bytes / 1073741824, 1) "
        "WHERE filesize_bytes IS NOT NULL"
    )
